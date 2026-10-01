"""Agent-callable integration onto a campaign's feature branch.

``integrate`` lands prepared candidates on the plane's ``main_branch`` (the
campaign feature branch):

* candidates are ordered by the existing wave planner, then merged with
  ``git merge --no-ff`` (one merge commit per unit, branches kept);
* the plane's trusted checks run on the combined tree, fingerprint-cached;
* candidates move to ``landed`` and units to ``landed``, keeping branches for
  provenance;
* a merge conflict aborts the merge and returns structured findings, never
  leaving the feature branch half-merged;
* re-running is a no-op: landed candidates are skipped, and a candidate whose
  branch is already contained in the feature branch is marked landed.

A **safety rail** refuses to integrate when ``main_branch`` equals the plane's
recorded default branch (captured once at init, §6.1).  Promotion to the
default branch stays a human ``git`` step.

When ``check_only`` is set (the orchestrator's ``verify`` step), the node's
acceptance commands run at the candidate commit, the verdict is recorded with
source ``node:<id>``, and nothing is merged.

This module also owns candidate integration ordering and simulation: prepared
candidates are grouped into the campaign DAG's wave order, and ``simulate``
materializes each wave's combined tree so the plane's trusted checks can run
once over the combined result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import campaign, gitutil
from .ownership import DEFAULT_WAVE_SIZE, plan_dag_waves
from .store import Store
from .util import SlicemeError, scratch_dir, worktrees_dir
from .verifier import (
    CheckResult,
    VerificationResult,
    acceptance_checks,
    compute_fingerprint,
    run_checks,
    verify_node,
)

__all__ = [
    "LandResult",
    "Wave",
    "found_default_branch",
    "integrate",
    "plan_waves",
    "record_node_verification",
    "simulate",
]


# ---------------------------------------------------------------------------
# Integration primitives
# ---------------------------------------------------------------------------
@dataclass
class LandResult:
    candidate_id: int
    unit_name: str
    branch: str
    status: str  # landed | failed | skipped
    detail: str = ""
    merge_commit: str | None = None
    checks: list[CheckResult] = field(default_factory=list)
    already_up_to_date: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate_id,
            "unit": self.unit_name,
            "branch": self.branch,
            "status": self.status,
            "detail": self.detail,
            "merge_commit": self.merge_commit,
            "already_up_to_date": self.already_up_to_date,
            "checks": [c.to_dict() for c in self.checks],
        }


def checks_output(checks: list[CheckResult]) -> str:
    lines = []
    for check in checks:
        lines.append(f"[{check.status}] {check.name}: {check.command}")
        if check.output:
            lines.append(check.output[-2000:])
    return "\n".join(lines) or "(no checks configured)"


def main_branch_of(config: dict[str, Any]) -> str:
    return config.get("main_branch") or "main"


def main_worktree(root: Path, branch: str) -> tuple[Path, bool]:
    """Return the worktree checked out at *branch*, creating ``_integration``.

    The second element is ``True`` when the worktree was just created.
    """
    entry = gitutil.worktree_for_branch(root, branch)
    if entry is not None:
        return entry.path, False
    path = worktrees_dir(root) / "_integration"
    # Clear any leftover directory *and* stale metadata before (re)creating it,
    # otherwise ``git worktree add`` fails with "already registered".
    gitutil.cleanup_worktree(root, path)
    gitutil.add_worktree(root, path, branch=branch, base=branch, new_branch=False)
    return path, True


def mark_landed(store: Store, candidate: dict[str, Any], merge_commit: str) -> None:
    cid = int(candidate["id"])
    store.update_candidate(cid, status="landed", head_commit=merge_commit)
    unit = store.get_unit(int(candidate["unit_id"]))
    if unit:
        store.set_unit_state(int(unit["id"]), "landed")
    store.conn.commit()


def conflict_summary(result: gitutil.GitResult) -> str:
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    for line in text.splitlines():
        line = line.strip()
        if (
            line.startswith("CONFLICT")
            or "Automatic merge failed" in line
            or "would be overwritten" in line
        ):
            return line
    return text.strip().splitlines()[-1] if text.strip() else "merge failed"


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
    return result.status == "passed", result


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
        if c["unit_name"] == node
        or c.get("node") == node
        or str(c["unit_id"]) == str(node)
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
        raise SlicemeError(
            f"refusing to integrate onto the plane's default branch '{main_branch}'; "
            "campaigns must set a feature branch at init "
            "(`sliceme start --no-unit --main feat/... --base ...`)"
        )

    acceptance = list(acceptance or [])
    results: list[LandResult] = []

    if node is not None:
        candidate, already_landed = _candidate_for_node(store, node)
        if candidate is None:
            raise SlicemeError(f"no candidate for node '{node}'")
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
        raise SlicemeError(
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
            message=f"sliceme integrate {candidate['branch']}",
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


# ---------------------------------------------------------------------------
# Candidate ordering and combined-tree simulation
# ---------------------------------------------------------------------------
@dataclass
class Wave:
    index: int
    candidates: list[dict[str, Any]] = field(default_factory=list)
    combined: str = ""
    check_status: str | None = None
    checks: list[CheckResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wave": self.index,
            "combined": self.combined,
            "members": [
                {"candidate": c["id"], "unit": c["unit_name"], "branch": c["branch"]}
                for c in self.candidates
            ],
            "check_status": self.check_status,
            "checks": [c.to_dict() for c in self.checks],
        }


def _wave_index_by_node(root: Path, config: dict[str, Any]) -> dict[str, int]:
    """Map every DAG node id to its wave index (empty for a non-campaign plane)."""
    branch = config.get("main_branch")
    dag = campaign.load_dag(root, branch) if branch else None
    if not dag or not dag.get("nodes"):
        return {}
    wave_size = int(dag.get("concurrency") or DEFAULT_WAVE_SIZE)
    planned = plan_dag_waves(list(dag["nodes"]), wave_size=wave_size)
    return {member: w.index for w in planned for member in w.members}


def plan_waves(
    store: Store,
    root: Path,
    config: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[Wave]:
    """Group candidates into DAG waves, each internally priority-ordered."""
    if not candidates:
        return []
    wave_of = _wave_index_by_node(root, config)
    fallback = (max(wave_of.values()) + 1) if wave_of else 0

    def key(candidate: dict[str, Any]) -> tuple[Any, ...]:
        node = str(candidate.get("node") or candidate.get("unit_name") or "")
        return (
            wave_of.get(node, fallback),
            -int(candidate.get("priority") or 0),
            float(candidate.get("created_at") or 0.0),
            int(candidate["id"]),
        )

    ordered = sorted(candidates, key=key)
    waves: list[Wave] = []
    by_index: dict[int, Wave] = {}
    for candidate in ordered:
        node = str(candidate.get("node") or candidate.get("unit_name") or "")
        index = wave_of.get(node, fallback)
        wave = by_index.get(index)
        if wave is None:
            wave = Wave(index=index)
            by_index[index] = wave
            waves.append(wave)
        wave.candidates.append(candidate)
    waves.sort(key=lambda w: w.index)
    return waves


def ordered_candidates(
    store: Store, root: Path, config: dict[str, Any], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Flatten the wave plan into a merge order, preserving every member."""
    waves = plan_waves(store, root, config, candidates)
    ordered: list[dict[str, Any]] = []
    seen: set[int] = set()
    for wave in waves:
        for candidate in wave.candidates:
            cid = int(candidate["id"])
            if cid not in seen:
                seen.add(cid)
                ordered.append(candidate)
    for candidate in candidates:
        cid = int(candidate["id"])
        if cid not in seen:
            ordered.append(candidate)
    return ordered


def _synthetic_commit(root: Path, tree: str, parents: list[str], message: str) -> str:
    args = ["commit-tree", tree]
    for parent in parents:
        args += ["-p", parent]
    args += ["-m", message]
    return gitutil.git(root, *args, check=True).stdout.strip()


def _merge_into_wave(root: Path, combined: str, branch: str) -> tuple[bool, str]:
    outcome = gitutil.merge_tree(root, combined, branch)
    if not outcome.clean or not outcome.tree:
        return False, combined
    new_ref = _synthetic_commit(
        root, outcome.tree, [combined, gitutil.rev_parse(root, branch)], "sliceme wave combine"
    )
    return True, new_ref


def simulate(
    store: Store,
    root: Path,
    config: dict[str, Any],
    *,
    statuses: list[str] | None = None,
    run_checks_flag: bool = True,
) -> dict[str, Any]:
    candidates = store.list_candidates(statuses=statuses or ["prepared", "pending"])
    waves = plan_waves(store, root, config, candidates)
    base_ref = config.get("base") or config.get("main_branch") or "main"
    scratch = scratch_dir(root)
    for wave in waves:
        combined = gitutil.rev_parse(root, base_ref)
        for candidate in wave.candidates:
            ok, combined = _merge_into_wave(root, combined, candidate["branch"])
            if not ok:
                combined = ""
                break
        wave.combined = combined
        if not run_checks_flag or not wave.combined:
            continue
        path = scratch / f"wave-{wave.index}-{abs(hash(wave.combined)) % 10_000_000}"
        try:
            gitutil.add_detached_worktree(root, path, wave.combined)
            status, results, _duration = run_checks(root, config, wave.combined, worktree=path)
            wave.check_status = status
            wave.checks = results
        except SlicemeError:
            wave.check_status = "error"
        finally:
            gitutil.cleanup_worktree(root, path)
    return {
        "candidate_count": len(candidates),
        "waves": [w.to_dict() for w in waves],
        "overall": _overall_status(waves),
    }


def _overall_status(waves: list[Wave]) -> str:
    for wave in waves:
        if wave.check_status not in (None, "passed"):
            return "failed"
    return "pass"

