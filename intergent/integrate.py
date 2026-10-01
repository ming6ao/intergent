"""Agent-callable integration onto a campaign's feature branch.

``integrate`` lands prepared candidates on the plane's ``main_branch`` (the
campaign feature branch):

* candidates are ordered by the existing wave planner, then merged with
  ``git merge --no-ff`` (one merge commit per unit, branches kept);
* the plane's trusted checks run on the combined tree, fingerprint-cached;
* candidates move to ``landed``, units to ``landed``, leases release;
* a merge conflict aborts the merge and returns structured findings, never
  leaving the feature branch half-merged;
* re-running is a no-op: landed candidates are skipped, and a candidate whose
  branch is already contained in the feature branch is marked landed.

A **safety rail** refuses to integrate when ``main_branch`` equals the plane's
recorded default branch (captured once at init, §6.1).  Promotion to the
default branch stays a human ``git`` step.

When ``check_only`` is set (the orchestrator's ``verify`` step), the node's
acceptance commands run at the candidate commit, the verdict is recorded with
source ``node:<id>`` (§6.4), and nothing is merged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import gitutil
from .commitops import (
    LandResult,
    checks_output,
    conflict_summary,
    main_branch_of,
    main_worktree,
    mark_landed,
    ordered_candidates,
)
from .store import Store
from .util import IntergentError
from .verifier import (
    CheckResult,
    VerificationResult,
    acceptance_checks,
    compute_fingerprint,
    run_checks,
    verify_node,
)

__all__ = ["integrate", "record_node_verification", "found_default_branch"]


def found_default_branch(root: Path, *, exclude: str | None = None) -> str:
    """Derive the repository's default branch.

    Used as a fallback for planes created before the value was recorded.
    Precedence: ``refs/remotes/origin/HEAD``, ``init.defaultBranch``, the first
    existing branch among ``main``/``master`` (skipping *exclude*), then
    ``main``.

    The checked-out branch is deliberately **not** a fallback: ``start`` adopts
    the current branch as the campaign feature branch, so at plane-init time the
    current branch is the feature branch, never the default.
    """
    origin = gitutil.git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if origin.ok and origin.stdout.strip():
        return origin.stdout.strip().removeprefix("origin/")
    configured = gitutil.git(root, "config", "--get", "init.defaultBranch", check=False)
    if configured.ok and configured.stdout.strip():
        name = configured.stdout.strip()
        if name != exclude:
            return name
    for candidate in ("main", "master"):
        if candidate != exclude and gitutil.branch_exists(root, candidate):
            return candidate
    return "main"


def _recorded_default(config: dict[str, Any], root: Path, *, exclude: str | None = None) -> str:
    recorded = config.get("default_branch")
    if recorded:
        return str(recorded)
    return found_default_branch(root, exclude=exclude)


def _verify_and_record_plane(
    store: Store,
    root: Path,
    config: dict[str, Any],
    candidate: dict[str, Any],
    commit: str,
    *,
    worktree: Path | None = None,
) -> tuple[bool, list[CheckResult]]:
    """Run (or reuse) the plane check vector for *commit* and persist it."""
    cid = int(candidate["id"])
    fingerprint = compute_fingerprint(root, config, commit)
    fp_id = store.get_or_create_fingerprint(
        cid,
        fingerprint.fingerprint,
        fingerprint.tree,
        fingerprint.cmd_digest,
        fingerprint.toolchain_digest,
        fingerprint.policy_digest,
        source=fingerprint.source,
    )
    cached = store.latest_verification_for_fingerprint(fp_id)
    if cached is not None:
        checks = [
            CheckResult(
                name="cache",
                command="(cached)",
                status=str(cached["status"]),
                returncode=0,
                output=cached.get("output") or "",
                duration=float(cached.get("duration") or 0.0),
            )
        ]
        return cached["status"] == "passed", checks
    status, checks, duration = run_checks(root, config, commit, worktree=worktree)
    store.add_verification(
        cid,
        fp_id,
        status,
        checks_output(checks),
        duration,
        commands=fingerprint.commands,
    )
    store.conn.commit()
    return status == "passed", checks


def record_node_verification(
    store: Store,
    root: Path,
    config: dict[str, Any],
    candidate: dict[str, Any],
    acceptance: list[str],
    *,
    node: str,
    gpu: str = "none",
    worktree: Path | None = None,
) -> tuple[bool, VerificationResult]:
    """Record a node's acceptance verdict, reusing a cached fingerprint (§6.4).

    Returns ``(passed, result)``; when a passing verdict already exists for the
    same ``(tree, acceptance, toolchain, policy, source)`` fingerprint the
    commands are not re-run.
    """
    source = f"node:{node}"
    head = gitutil.rev_parse(root, candidate["branch"])
    fingerprint = compute_fingerprint(
        root, config, head, checks=acceptance_checks(acceptance), source=source
    )
    fp_id = store.get_or_create_fingerprint(
        int(candidate["id"]),
        fingerprint.fingerprint,
        fingerprint.tree,
        fingerprint.cmd_digest,
        fingerprint.toolchain_digest,
        fingerprint.policy_digest,
        source=fingerprint.source,
    )
    cached = store.latest_verification_for_fingerprint(fp_id)
    if cached is not None:
        result = VerificationResult(
            status=str(cached["status"]),
            fingerprint=fingerprint,
            checks=[],
            from_cache=True,
            duration=float(cached.get("duration") or 0.0),
        )
        return cached["status"] == "passed", result
    result = verify_node(
        root, config, head, acceptance, source=source, gpu=gpu, worktree=worktree
    )
    store.add_verification(
        int(candidate["id"]),
        fp_id,
        result.status,
        checks_output(result.checks),
        result.duration,
        commands=fingerprint.commands,
        gpu=gpu,
    )
    store.conn.commit()
    store.event(
        "verification.node",
        candidate_id=int(candidate["id"]),
        data={"node": node, "source": source, "status": result.status, "gpu": gpu},
    )
    return result.status == "passed", result


# Imported here to avoid a circular import at module load.


def _candidate_for_node(
    store: Store, node: str
) -> tuple[dict[str, Any] | None, bool]:
    """Resolve the latest candidate for a node/unit id.

    Returns ``(candidate, already_landed)``.  A landed candidate is reported as
    already integrated so a repeated ``--node`` call is a no-op.
    """
    candidates = [
        c
        for c in store.list_candidates()
        if c["unit_name"] == node or str(c["unit_id"]) == str(node)
    ]
    if not candidates:
        return None, False
    prepared = [c for c in candidates if c["status"] == "prepared"]
    if prepared:
        return prepared[-1], False
    return candidates[-1], True


def integrate(
    store: Store,
    root: Path,
    config: dict[str, Any],
    *,
    node: str | None = None,
    acceptance: list[str] | None = None,
    gpu: str = "none",
    check_only: bool = False,
    run_checks_flag: bool = True,
) -> list[LandResult]:
    main_branch = main_branch_of(config)
    default_branch = _recorded_default(config, root, exclude=main_branch)
    if main_branch == default_branch:
        raise IntergentError(
            f"refusing to integrate onto the plane's default branch '{main_branch}'; "
            "campaigns must set a feature branch at init "
            "(`intergent start --no-unit --main feat/... --base ...`)"
        )

    acceptance = list(acceptance or [])
    results: list[LandResult] = []

    if node is not None:
        candidate, already_landed = _candidate_for_node(store, node)
        if candidate is None:
            raise IntergentError(f"no candidate for node '{node}'")
        candidates = [] if already_landed else [candidate]
        if already_landed:
            results.append(
                LandResult(
                    candidate_id=int(candidate["id"]),
                    unit_name=candidate["unit_name"],
                    branch=candidate["branch"],
                    status="landed",
                    detail="already integrated",
                    already_up_to_date=True,
                )
            )
    else:
        candidates = store.list_candidates(statuses=["prepared"])

    # The wave planner merges onto the plane's ``base``; for a campaign the
    # integration target is the feature branch, so plan against that instead.
    wave_config = {**config, "base": main_branch}

    if check_only:
        for candidate in ordered_candidates(store, root, wave_config, candidates):
            if acceptance:
                passed, result = record_node_verification(
                    store,
                    root,
                    config,
                    candidate,
                    acceptance,
                    node=node or candidate["unit_name"],
                    gpu=gpu,
                )
                results.append(
                    LandResult(
                        candidate_id=int(candidate["id"]),
                        unit_name=candidate["unit_name"],
                        branch=candidate["branch"],
                        status="passed" if passed else "failed",
                        detail="node acceptance " + ("passed" if passed else "failed"),
                        checks=result.checks,
                    )
                )
            elif run_checks_flag:
                head = gitutil.rev_parse(root, candidate["branch"])
                passed, checks = _verify_and_record_plane(
                    store, root, config, candidate, head
                )
                results.append(
                    LandResult(
                        candidate_id=int(candidate["id"]),
                        unit_name=candidate["unit_name"],
                        branch=candidate["branch"],
                        status="passed" if passed else "failed",
                        detail="plane checks " + ("passed" if passed else "failed"),
                        checks=checks,
                    )
                )
        return results

    ordered = ordered_candidates(store, root, wave_config, candidates)
    if not ordered:
        return results

    wt_path, _created = main_worktree(root, main_branch)
    if not gitutil.is_clean(wt_path):
        raise IntergentError(
            f"integration worktree {wt_path} is dirty; commit or discard changes before integrate"
        )

    for candidate in ordered:
        head = gitutil.rev_parse(root, candidate["branch"])
        feature_head = gitutil.head_commit(wt_path)
        if gitutil.merge_base(root, feature_head, head) == head:
            mark_landed(store, candidate, feature_head)
            results.append(
                LandResult(
                    candidate_id=int(candidate["id"]),
                    unit_name=candidate["unit_name"],
                    branch=candidate["branch"],
                    status="landed",
                    detail="already contained in the feature branch",
                    merge_commit=feature_head,
                    already_up_to_date=True,
                )
            )
            continue

        pre_merge = feature_head

        # Node acceptance first (when the orchestrator passes it), so a failing
        # acceptance never advances the feature branch.
        if acceptance:
            passed, result = record_node_verification(
                store,
                root,
                config,
                candidate,
                acceptance,
                node=node or candidate["unit_name"],
                gpu=gpu,
            )
            if not passed:
                results.append(
                    LandResult(
                        candidate_id=int(candidate["id"]),
                        unit_name=candidate["unit_name"],
                        branch=candidate["branch"],
                        status="failed",
                        detail="node acceptance failed",
                        checks=result.checks,
                    )
                )
                break

        merge = gitutil.merge_into(
            wt_path,
            candidate["branch"],
            message=f"intergent integrate {candidate['branch']}",
            no_ff=True,
        )
        if not merge.ok:
            gitutil.merge_abort(wt_path)
            results.append(
                LandResult(
                    candidate_id=int(candidate["id"]),
                    unit_name=candidate["unit_name"],
                    branch=candidate["branch"],
                    status="failed",
                    detail="merge conflict: " + conflict_summary(merge),
                )
            )
            break

        merge_commit = gitutil.head_commit(wt_path)
        checks: list[CheckResult] = []
        if run_checks_flag:
            passed, checks = _verify_and_record_plane(
                store, root, config, candidate, merge_commit
            )
            if not passed:
                gitutil.reset_hard(wt_path, pre_merge)
                store.event(
                    "integrate.checks_failed",
                    candidate_id=int(candidate["id"]),
                    data={"merge_commit": merge_commit},
                )
                store.conn.commit()
                results.append(
                    LandResult(
                        candidate_id=int(candidate["id"]),
                        unit_name=candidate["unit_name"],
                        branch=candidate["branch"],
                        status="failed",
                        detail="combined checks failed; feature branch restored",
                        checks=checks,
                    )
                )
                break

        mark_landed(store, candidate, merge_commit)
        store.event(
            "integrate.landed",
            candidate_id=int(candidate["id"]),
            data={"merge_commit": merge_commit, "main_branch": main_branch},
        )
        store.conn.commit()
        results.append(
            LandResult(
                candidate_id=int(candidate["id"]),
                unit_name=candidate["unit_name"],
                branch=candidate["branch"],
                status="landed",
                detail=f"merged into {main_branch}",
                merge_commit=merge_commit,
                checks=checks,
            )
        )
    return results
