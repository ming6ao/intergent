"""Approval-gated landing onto the local main branch.

Landing is transactional per candidate: the merge is materialized and verified
in a scratch worktree first, and only then applied to the real main worktree.
Waves are processed in order so dependency and mergeability ordering is
respected (``docs/review-workflow.md`` section 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitutil
from .planner import plan_waves
from .store import Store
from .util import IntergentError, scratch_dir, worktrees_dir
from .verifier import CheckResult, compute_fingerprint, run_checks


@dataclass
class LandResult:
    candidate_id: int
    unit_name: str
    branch: str
    status: str  # landed | blocked | skipped | failed
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


def main_worktree(root: Path, branch: str) -> tuple[Path, bool]:
    entry = gitutil.worktree_for_branch(root, branch)
    if entry is not None:
        return entry.path, False
    path = worktrees_dir(root) / "_integration"
    if path.exists():
        gitutil.remove_worktree(root, path, force=True)
    gitutil.add_worktree(root, path, branch=branch, base=branch, new_branch=False)
    return path, True


def _ordered_approved(
    store: Store, root: Path, config: dict[str, Any], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
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


def land_candidates(
    store: Store,
    root: Path,
    config: dict[str, Any],
    candidate_ids: list[int],
    *,
    run_checks_flag: bool = True,
    cleanup: bool = False,
) -> list[LandResult]:
    main_branch = config.get("main_branch") or "main"
    approved = [
        c
        for c in store.list_candidates(statuses=["approved"])
        if int(c["id"]) in set(candidate_ids)
    ]
    missing = set(candidate_ids) - {int(c["id"]) for c in approved}
    results: list[LandResult] = []
    for cid in sorted(missing):
        candidate = store.get_candidate(cid)
        results.append(
            LandResult(
                candidate_id=cid,
                unit_name=(candidate or {}).get("unit_name", "?"),
                branch=(candidate or {}).get("branch", "?"),
                status="skipped",
                detail="candidate is not approved",
            )
        )
    if not approved:
        return results

    wt_path, created = main_worktree(root, main_branch)
    if not gitutil.is_clean(wt_path):
        raise IntergentError(
            f"main worktree {wt_path} is dirty; commit or discard changes before landing"
        )

    ordered = _ordered_approved(store, root, config, approved)
    for candidate in ordered:
        result = _land_one(
            store, root, config, candidate, wt_path, run_checks_flag=run_checks_flag
        )
        results.append(result)
        if result.status != "landed":
            # Stop the train: later candidates may depend on this one, and the
            # main tip moved (or failed to).  The user can re-run after fixing.
            break
        if cleanup:
            _cleanup_unit(store, root, candidate)

    gitutil.prune_worktrees(root)
    return results


def _land_one(
    store: Store,
    root: Path,
    config: dict[str, Any],
    candidate: dict[str, Any],
    main_wt: Path,
    *,
    run_checks_flag: bool,
) -> LandResult:
    cid = int(candidate["id"])
    unit_name = candidate["unit_name"]
    branch = candidate["branch"]
    result = LandResult(candidate_id=cid, unit_name=unit_name, branch=branch, status="blocked")

    # Nothing to do already?
    head = gitutil.rev_parse(root, branch)
    main_head = gitutil.head_commit(main_wt)
    if gitutil.merge_base(root, main_head, head) == head:
        result.status = "landed"
        result.detail = "already contained in main"
        result.already_up_to_date = True
        result.merge_commit = main_head
        _mark_landed(store, candidate, main_head)
        return result

    scratch = scratch_dir(root) / f"land-{cid}-{abs(hash(head)) % 10_000_000}"
    try:
        if scratch.exists():
            gitutil.remove_worktree(root, scratch, force=True)
        base_commit = gitutil.rev_parse(root, main_branch_of(config))
        gitutil.add_detached_worktree(root, scratch, base_commit)
        merge = gitutil.merge_into(
            scratch, branch, message=f"ig trial merge {branch}", no_ff=True
        )
        if not merge.ok:
            gitutil.merge_abort(scratch)
            result.detail = f"merge conflict against main: {_conflict_summary(merge)}"
            store.update_candidate(cid, status="blocked")
            store.event("land.blocked", candidate_id=cid, data={"reason": result.detail})
            return result
        if run_checks_flag:
            status, checks, _duration = run_checks(root, config, head, worktree=scratch)
            result.checks = checks
            if status != "passed":
                result.detail = f"combined check {status} on merged tree"
                store.update_candidate(cid, status="failed")
                store.event("land.failed", candidate_id=cid, data={"reason": result.detail})
                return result
    finally:
        gitutil.remove_worktree(root, scratch, force=True)
        from .util import rmtree

        rmtree(scratch)
        gitutil.prune_worktrees(root)

    # Pre-verified; apply for real to the main worktree.
    if not gitutil.is_clean(main_wt):
        result.detail = f"main worktree {main_wt} became dirty; refusing to merge"
        return result
    real = gitutil.merge_into(
        main_wt,
        branch,
        message=f"ig: land {unit_name} ({branch})",
        no_ff=True,
    )
    if not real.ok:
        gitutil.merge_abort(main_wt)
        result.detail = f"real merge failed: {_conflict_summary(real)}"
        store.update_candidate(cid, status="blocked")
        return result

    new_head = gitutil.head_commit(main_wt)
    result.status = "landed"
    result.merge_commit = new_head
    result.detail = "merged into " + main_branch_of(config)

    if run_checks_flag:
        # Record the merged-result verification against the candidate so the
        # review packet carries landing evidence too.
        fp = compute_fingerprint(root, config, new_head)
        fp_id = store.get_or_create_fingerprint(
            cid, fp.fingerprint, fp.tree, fp.cmd_digest, fp.toolchain_digest, fp.policy_digest
        )
        store.add_verification(
            cid, fp_id, "passed", "verified merged result during landing", 0.0
        )

    _mark_landed(store, candidate, new_head)
    store.event("land.landed", candidate_id=cid, data={"merge_commit": new_head})
    return result


def _mark_landed(store: Store, candidate: dict[str, Any], merge_commit: str) -> None:
    cid = int(candidate["id"])
    store.update_candidate(cid, status="landed", head_commit=merge_commit)
    unit = store.get_unit(int(candidate["unit_id"]))
    if unit:
        store.set_unit_state(int(unit["id"]), "landed")
        store.release_claims(int(unit["id"]))
    intent_id = candidate.get("intent_id")
    if intent_id:
        store.set_intent_status(int(intent_id), "released")
    store.conn.commit()


def _cleanup_unit(store: Store, root: Path, candidate: dict[str, Any]) -> None:
    unit = store.get_unit(int(candidate["unit_id"]))
    if not unit:
        return
    path = Path(unit["worktree"])
    gitutil.remove_worktree(root, path, force=True)
    from .util import rmtree

    rmtree(path)
    branch = unit["branch"]
    # Keep the branch (history stays reachable) but drop the worktree.
    store.event("unit.cleaned", unit_id=int(unit["id"]), data={"branch": branch})


def main_branch_of(config: dict[str, Any]) -> str:
    return config.get("main_branch") or "main"


def _conflict_summary(result: gitutil.GitResult) -> str:
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("CONFLICT") or "Automatic merge failed" in line or "would be overwritten" in line:
            return line
    return text.strip().splitlines()[-1] if text.strip() else "merge failed"
