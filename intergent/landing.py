"""Approval-gated landing onto the local main branch.

Landing is transactional per wave: the combined result is materialized and
verified in a scratch worktree first, then *drafted* into the real main
worktree as staged (uncommitted) changes.  A human reviews the draft and then
either commits it (``--commit``) or discards it (``--abort``).  Waves are
processed in order so dependency and mergeability ordering is respected
(``docs/review-workflow.md`` section 5).

Two modes (``landing.mode``):

- ``draft`` (default): prepare the staged draft on main and wait for approval;
- ``direct``: apply and commit in one step (no human draft review).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitutil
from .planner import plan_waves
from .store import Store
from .util import (
    IntergentError,
    editor_command,
    now,
    scratch_dir,
    worktrees_dir,
)
from .verifier import CheckResult, compute_fingerprint, run_checks

DRAFT_META_KEY = "landing_draft"


@dataclass
class LandResult:
    candidate_id: int
    unit_name: str
    branch: str
    status: str  # drafted | landed | aborted | blocked | skipped | failed
    detail: str = ""
    merge_commit: str | None = None
    checks: list[CheckResult] = field(default_factory=list)
    already_up_to_date: bool = False
    strategy: str = "merge"
    draft: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate_id,
            "unit": self.unit_name,
            "branch": self.branch,
            "status": self.status,
            "detail": self.detail,
            "merge_commit": self.merge_commit,
            "already_up_to_date": self.already_up_to_date,
            "strategy": self.strategy,
            "draft": self.draft,
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
    draft: bool | None = None,
    commit_draft: bool = False,
    abort_draft: bool = False,
) -> list[LandResult]:
    pending = store.get_meta(DRAFT_META_KEY)
    if commit_draft:
        if not pending:
            raise IntergentError("no landing draft is pending")
        return _finalize_draft(
            store, root, config, pending, run_checks_flag=run_checks_flag, cleanup=cleanup
        )
    if abort_draft:
        if not pending:
            raise IntergentError("no landing draft is pending")
        return [_abort_draft(store, root, config, pending)]
    if pending:
        raise IntergentError(
            "a landing draft is staged on main; commit it with `review --land --commit` "
            "or discard it with `review --land --abort`"
        )

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

    mode = "draft" if draft is True else "direct" if draft is False else landing_mode(config)
    if mode == "draft":
        return _draft_wave(
            store, root, config, approved, run_checks_flag=run_checks_flag, results=results
        )

    wt_path, created = main_worktree(root, main_branch_of(config))
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


# ---------------------------------------------------------------------------
# Draft mode
# ---------------------------------------------------------------------------


def _draft_wave(
    store: Store,
    root: Path,
    config: dict[str, Any],
    approved: list[dict[str, Any]],
    *,
    run_checks_flag: bool,
    results: list[LandResult],
) -> list[LandResult]:
    strategy = landing_strategy(config)
    main_branch = main_branch_of(config)
    wt_path, _created = main_worktree(root, main_branch)
    if not gitutil.is_clean(wt_path):
        raise IntergentError(
            f"main worktree {wt_path} is dirty; commit or discard changes before landing"
        )

    ordered = _ordered_approved(store, root, config, approved)
    base_commit = gitutil.head_commit(wt_path)
    scratch = scratch_dir(root) / f"draft-{abs(hash(base_commit)) % 10_000_000}"
    staged: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    try:
        if scratch.exists():
            gitutil.remove_worktree(root, scratch, force=True)
        gitutil.add_detached_worktree(root, scratch, base_commit)
        for candidate in ordered:
            cid = int(candidate["id"])
            branch = candidate["branch"]
            head = gitutil.rev_parse(root, branch)
            scratch_head = gitutil.head_commit(scratch)
            if gitutil.merge_base(root, scratch_head, head) == head:
                _mark_landed(store, candidate, base_commit)
                results.append(
                    LandResult(
                        candidate_id=cid,
                        unit_name=candidate["unit_name"],
                        branch=branch,
                        status="landed",
                        detail="already contained in main",
                        merge_commit=base_commit,
                        already_up_to_date=True,
                        strategy=strategy,
                    )
                )
                continue
            merge = gitutil.merge_squash_into(
                scratch, branch, message=f"ig draft {branch}"
            )
            if not merge.ok:
                gitutil.merge_abort(scratch)
                blocked.append(
                    {
                        "id": cid,
                        "unit": candidate["unit_name"],
                        "reason": f"merge conflict against main: {_conflict_summary(merge)}",
                    }
                )
                break
            staged.append(candidate)
        if not staged:
            for entry in blocked:
                store.update_candidate(int(entry["id"]), status="blocked")
                store.event(
                    "land.blocked", candidate_id=int(entry["id"]), data={"reason": entry["reason"]}
                )
                results.append(
                    LandResult(
                        candidate_id=int(entry["id"]),
                        unit_name=entry["unit"],
                        branch="",
                        status="blocked",
                        detail=entry["reason"],
                        strategy=strategy,
                    )
                )
            return results

        combined = gitutil.head_commit(scratch)
        checks: list[CheckResult] = []
        if run_checks_flag:
            status, checks, _duration = run_checks(root, config, combined, worktree=scratch)
            if status != "passed":
                detail = f"combined check {status} on drafted tree"
                for candidate in staged:
                    store.update_candidate(int(candidate["id"]), status="failed")
                    store.event(
                        "land.failed",
                        candidate_id=int(candidate["id"]),
                        data={"reason": detail},
                    )
                return [
                    LandResult(
                        candidate_id=int(candidate["id"]),
                        unit_name=candidate["unit_name"],
                        branch=candidate["branch"],
                        status="failed",
                        detail=detail,
                        checks=checks,
                        strategy=strategy,
                    )
                    for candidate in staged
                ]

        applied = gitutil.read_tree_reset(wt_path, combined)
        if not applied.ok:
            raise IntergentError(
                "failed to stage the landing draft on main: "
                + _conflict_summary(applied)
            )

        draft = {
            "base_commit": base_commit,
            "combined_commit": combined,
            "main_branch": main_branch,
            "main_worktree": str(wt_path),
            "message": _draft_message(staged),
            "files": gitutil.diff_changed(root, base_commit, combined),
            "stat": gitutil.diff_stat(root, base_commit, combined),
            "open_command": editor_command(wt_path, config),
            "candidates": [
                {
                    "id": int(c["id"]),
                    "unit": c["unit_name"],
                    "branch": c["branch"],
                    "summary": c.get("summary"),
                }
                for c in staged
            ],
            "blocked": blocked,
            "created_at": now(),
        }
        store.set_meta(DRAFT_META_KEY, draft)
        store.event(
            "land.drafted",
            data={"candidates": [int(c["id"]) for c in staged], "base": base_commit},
        )
        first = staged[0]
        results.append(
            LandResult(
                candidate_id=int(first["id"]),
                unit_name=", ".join(c["unit_name"] for c in staged),
                branch=first["branch"],
                status="drafted",
                detail=f"drafted {len(staged)} candidate(s) onto {main_branch}; awaiting approval",
                checks=checks,
                strategy=strategy,
                draft=draft,
            )
        )
        for entry in blocked:
            results.append(
                LandResult(
                    candidate_id=int(entry["id"]),
                    unit_name=entry["unit"],
                    branch="",
                    status="blocked",
                    detail=entry["reason"],
                    strategy=strategy,
                )
            )
        return results
    finally:
        gitutil.remove_worktree(root, scratch, force=True)
        from .util import rmtree

        rmtree(scratch)
        gitutil.prune_worktrees(root)


def _draft_message(staged: list[dict[str, Any]]) -> str:
    subject = "ig: land " + ", ".join(c["unit_name"] for c in staged)
    lines = [subject]
    details = [
        f"- {c['unit_name']}: {c.get('summary')}" for c in staged if c.get("summary")
    ]
    if details:
        lines.append("")
        lines.extend(details)
    return "\n".join(lines)


def _finalize_draft(
    store: Store,
    root: Path,
    config: dict[str, Any],
    draft: dict[str, Any],
    *,
    run_checks_flag: bool,
    cleanup: bool,
) -> list[LandResult]:
    wt_path = Path(draft["main_worktree"])
    if gitutil.head_commit(wt_path) != draft["base_commit"]:
        raise IntergentError(
            "main moved since the landing draft was prepared; abort it and draft again"
        )
    commit = gitutil.commit_all(wt_path, draft["message"])
    if not commit.ok:
        raise IntergentError(
            "failed to commit the landing draft: " + (commit.stderr or commit.stdout).strip()
        )
    new_head = gitutil.head_commit(wt_path)
    strategy = landing_strategy(config)
    results: list[LandResult] = []
    for entry in draft.get("candidates", []):
        candidate = store.get_candidate(int(entry["id"]))
        if candidate is None:
            continue
        cid = int(candidate["id"])
        if run_checks_flag:
            fp = compute_fingerprint(root, config, new_head)
            fp_id = store.get_or_create_fingerprint(
                cid, fp.fingerprint, fp.tree, fp.cmd_digest, fp.toolchain_digest, fp.policy_digest
            )
            store.add_verification(
                cid, fp_id, "passed", "verified merged result during landing", 0.0
            )
        _mark_landed(store, candidate, new_head)
        store.event("land.landed", candidate_id=cid, data={"merge_commit": new_head})
        results.append(
            LandResult(
                candidate_id=cid,
                unit_name=entry.get("unit") or candidate.get("unit_name", "?"),
                branch=entry.get("branch") or candidate.get("branch", ""),
                status="landed",
                detail=f"squashed into {draft['main_branch']}",
                merge_commit=new_head,
                strategy=strategy,
            )
        )
        if cleanup:
            _cleanup_unit(store, root, candidate)
    for entry in draft.get("blocked", []):
        cid = int(entry["id"])
        store.update_candidate(cid, status="blocked")
        store.event("land.blocked", candidate_id=cid, data={"reason": entry["reason"]})
        results.append(
            LandResult(
                candidate_id=cid,
                unit_name=entry["unit"],
                branch="",
                status="blocked",
                detail=entry["reason"],
                strategy=strategy,
            )
        )
    store.set_meta(DRAFT_META_KEY, None)
    gitutil.prune_worktrees(root)
    return results


def _abort_draft(
    store: Store, root: Path, config: dict[str, Any], draft: dict[str, Any]
) -> LandResult:
    wt_path = Path(draft["main_worktree"])
    gitutil.reset_hard(wt_path, draft["base_commit"])
    store.set_meta(DRAFT_META_KEY, None)
    store.event("land.draft_aborted", data={"base": draft["base_commit"]})
    gitutil.prune_worktrees(root)
    return LandResult(
        candidate_id=int((draft.get("candidates") or [{"id": 0}])[0]["id"]),
        unit_name=", ".join(c.get("unit", "?") for c in draft.get("candidates", [])),
        branch="",
        status="aborted",
        detail="landing draft discarded; main restored",
        draft=draft,
    )


# ---------------------------------------------------------------------------
# Direct mode
# ---------------------------------------------------------------------------


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
    strategy = landing_strategy(config)
    result = LandResult(
        candidate_id=cid, unit_name=unit_name, branch=branch, status="blocked", strategy=strategy
    )

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
        if strategy == "squash":
            merge = gitutil.merge_squash_into(
                scratch, branch, message=f"ig trial squash {branch}"
            )
        else:
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
    real = _apply_to_main(main_wt, branch, unit_name, strategy)
    if not real.ok:
        gitutil.merge_abort(main_wt)
        result.detail = f"real merge failed: {_conflict_summary(real)}"
        store.update_candidate(cid, status="blocked")
        return result

    new_head = gitutil.head_commit(main_wt)
    result.status = "landed"
    result.merge_commit = new_head
    verb = "squashed" if strategy == "squash" else "merged"
    result.detail = f"{verb} into " + main_branch_of(config)

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


def _apply_to_main(
    main_wt: Path, branch: str, unit_name: str, strategy: str
) -> gitutil.GitResult:
    message = f"ig: land {unit_name} ({branch})"
    if strategy == "squash":
        return gitutil.merge_squash_into(main_wt, branch, message=message)
    return gitutil.merge_into(main_wt, branch, message=message, no_ff=True)


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


def landing_strategy(config: dict[str, Any]) -> str:
    """How landed candidates reach main: ``squash`` (default) or ``merge``."""
    strategy = (config.get("landing") or {}).get("strategy") or "squash"
    if strategy not in {"squash", "merge"}:
        raise IntergentError(
            f"unknown landing.strategy: {strategy!r} (expected 'squash' or 'merge')"
        )
    return strategy


def landing_mode(config: dict[str, Any]) -> str:
    """Whether landing stages a draft for approval (``draft``, default) or commits directly."""
    mode = (config.get("landing") or {}).get("mode") or "draft"
    if mode not in {"draft", "direct"}:
        raise IntergentError(
            f"unknown landing.mode: {mode!r} (expected 'draft' or 'direct')"
        )
    return mode


def _conflict_summary(result: gitutil.GitResult) -> str:
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("CONFLICT") or "Automatic merge failed" in line or "would be overwritten" in line:
            return line
    return text.strip().splitlines()[-1] if text.strip() else "merge failed"
