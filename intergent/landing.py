"""Approval-gated handoff onto the local main branch.

The *handoff* is the single boundary between agent work and human approval:

1. the agent's ``prepared`` candidates are verified and trial-merged into a
   scratch worktree;
2. the combined result is written onto the real main worktree as staged
   (uncommitted) changes, and the staged units move to ``pending``;
3. a human either **approves** -- one commit on main, units ``landed``,
   worktrees cleaned -- or **rejects** -- main restored, units back to
   ``working``.

There is no direct-commit mode: every landing goes through a draft.
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
    rmtree,
    scratch_dir,
    worktrees_dir,
)
from .verifier import CheckResult, compute_fingerprint, run_checks

DRAFT_META_KEY = "handoff_draft"


@dataclass
class LandResult:
    candidate_id: int
    unit_name: str
    branch: str
    status: str  # pending | landed | rejected | failed | skipped
    detail: str = ""
    merge_commit: str | None = None
    checks: list[CheckResult] = field(default_factory=list)
    already_up_to_date: bool = False
    strategy: str = "squash"
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


def _checks_output(checks: list[CheckResult]) -> str:
    lines = []
    for check in checks:
        lines.append(f"[{check.status}] {check.name}: {check.command}")
        if check.output:
            lines.append(check.output[-2000:])
    return "\n".join(lines) or "(no checks configured)"


def main_worktree(root: Path, branch: str) -> tuple[Path, bool]:
    entry = gitutil.worktree_for_branch(root, branch)
    if entry is not None:
        return entry.path, False
    path = worktrees_dir(root) / "_integration"
    if path.exists():
        gitutil.remove_worktree(root, path, force=True)
    gitutil.add_worktree(root, path, branch=branch, base=branch, new_branch=False)
    return path, True


def _ordered_candidates(
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


def pending_draft(store: Store) -> dict[str, Any] | None:
    return store.get_meta(DRAFT_META_KEY)


# ---------------------------------------------------------------------------
# Handoff (agent side)
# ---------------------------------------------------------------------------


def handoff(
    store: Store,
    root: Path,
    config: dict[str, Any],
    candidate_ids: list[int],
    *,
    run_checks_flag: bool = True,
) -> list[LandResult]:
    """Stage the prepared candidates as one uncommitted draft on main."""
    if pending_draft(store):
        raise IntergentError(
            "a handoff is already staged on main; approve or reject it with `review` first"
        )
    wanted = set(candidate_ids)
    prepared = [
        c for c in store.list_candidates(statuses=["prepared"]) if int(c["id"]) in wanted
    ]
    results: list[LandResult] = []
    for cid in sorted(wanted - {int(c["id"]) for c in prepared}):
        candidate = store.get_candidate(cid)
        results.append(
            LandResult(
                candidate_id=cid,
                unit_name=(candidate or {}).get("unit_name", "?"),
                branch=(candidate or {}).get("branch", "?"),
                status="skipped",
                detail="candidate is not prepared",
            )
        )
    if not prepared:
        return results
    return _stage_draft(
        store, root, config, prepared, run_checks_flag=run_checks_flag, results=results
    )


def _stage_draft(
    store: Store,
    root: Path,
    config: dict[str, Any],
    prepared: list[dict[str, Any]],
    *,
    run_checks_flag: bool,
    results: list[LandResult],
) -> list[LandResult]:
    main_branch = main_branch_of(config)
    wt_path, _created = main_worktree(root, main_branch)
    if not gitutil.is_clean(wt_path):
        raise IntergentError(
            f"main worktree {wt_path} is dirty; commit or discard changes before handoff"
        )

    ordered = _ordered_candidates(store, root, config, prepared)
    base_commit = gitutil.head_commit(wt_path)
    scratch = scratch_dir(root) / f"handoff-{abs(hash(base_commit)) % 10_000_000}"
    staged: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    failed: list[tuple[dict[str, Any], list[CheckResult]]] = []
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
                # Already contained in main: land it without a draft entry.
                _mark_landed(store, candidate, base_commit)
                _cleanup_unit(store, root, candidate)
                results.append(
                    LandResult(
                        candidate_id=cid,
                        unit_name=candidate["unit_name"],
                        branch=branch,
                        status="landed",
                        detail="already contained in main",
                        merge_commit=base_commit,
                        already_up_to_date=True,
                    )
                )
                continue
            if run_checks_flag:
                fp = compute_fingerprint(root, config, head)
                fp_id = store.get_or_create_fingerprint(
                    cid,
                    fp.fingerprint,
                    fp.tree,
                    fp.cmd_digest,
                    fp.toolchain_digest,
                    fp.policy_digest,
                )
                cached = store.latest_verification_for_fingerprint(fp_id)
                if cached is None or cached["status"] != "passed":
                    status, checks, duration = run_checks(root, config, head)
                    store.add_verification(cid, fp_id, status, _checks_output(checks), duration)
                    store.conn.commit()
                    if status != "passed":
                        failed.append((candidate, checks))
                        break
            merge = gitutil.merge_squash_into(scratch, branch, message=f"ig handoff {branch}")
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

        if failed:
            for candidate, checks in failed:
                results.append(
                    LandResult(
                        candidate_id=int(candidate["id"]),
                        unit_name=candidate["unit_name"],
                        branch=candidate["branch"],
                        status="failed",
                        detail="candidate checks failed",
                        checks=checks,
                    )
                )
            return results
        if not staged:
            for entry in blocked:
                results.append(
                    LandResult(
                        candidate_id=int(entry["id"]),
                        unit_name=entry["unit"],
                        branch="",
                        status="failed",
                        detail=entry["reason"],
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
                    results.append(
                        LandResult(
                            candidate_id=int(candidate["id"]),
                            unit_name=candidate["unit_name"],
                            branch=candidate["branch"],
                            status="failed",
                            detail=detail,
                            checks=checks,
                        )
                    )
                return results

        applied = gitutil.read_tree_reset(wt_path, combined)
        if not applied.ok:
            raise IntergentError(
                "failed to stage the handoff on main: " + _conflict_summary(applied)
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
        for candidate in staged:
            store.update_candidate(int(candidate["id"]), status="pending")
            unit = store.get_unit(int(candidate["unit_id"]))
            if unit:
                store.set_unit_state(int(unit["id"]), "pending")
        store.conn.commit()
        store.event(
            "handoff.staged",
            data={"candidates": [int(c["id"]) for c in staged], "base": base_commit},
        )
        first = staged[0]
        results.append(
            LandResult(
                candidate_id=int(first["id"]),
                unit_name=", ".join(c["unit_name"] for c in staged),
                branch=first["branch"],
                status="pending",
                detail=f"staged {len(staged)} candidate(s) onto {main_branch}; awaiting approval",
                checks=checks,
                draft=draft,
            )
        )
        for entry in blocked:
            results.append(
                LandResult(
                    candidate_id=int(entry["id"]),
                    unit_name=entry["unit"],
                    branch="",
                    status="failed",
                    detail=entry["reason"],
                )
            )
        return results
    finally:
        gitutil.remove_worktree(root, scratch, force=True)
        rmtree(scratch)
        gitutil.prune_worktrees(root)


# ---------------------------------------------------------------------------
# Approval (human side)
# ---------------------------------------------------------------------------


def finalize(
    store: Store,
    root: Path,
    config: dict[str, Any],
    *,
    approve: bool,
    cleanup: bool = True,
) -> list[LandResult]:
    draft = pending_draft(store)
    if not draft:
        raise IntergentError("no handoff is pending")
    if approve:
        return _finalize_draft(store, root, config, draft, cleanup=cleanup)
    return [_abort_draft(store, root, config, draft)]


def _finalize_draft(
    store: Store,
    root: Path,
    config: dict[str, Any],
    draft: dict[str, Any],
    *,
    cleanup: bool,
) -> list[LandResult]:
    wt_path = Path(draft["main_worktree"])
    if gitutil.head_commit(wt_path) != draft["base_commit"]:
        raise IntergentError(
            "main moved since the handoff was staged; reject it and hand off again"
        )
    commit = gitutil.commit_all(wt_path, draft["message"])
    if not commit.ok:
        raise IntergentError(
            "failed to commit the handoff: " + (commit.stderr or commit.stdout).strip()
        )
    new_head = gitutil.head_commit(wt_path)
    results: list[LandResult] = []
    for entry in draft.get("candidates", []):
        candidate = store.get_candidate(int(entry["id"]))
        if candidate is None:
            continue
        cid = int(candidate["id"])
        _mark_landed(store, candidate, new_head)
        store.event("handoff.landed", candidate_id=cid, data={"merge_commit": new_head})
        results.append(
            LandResult(
                candidate_id=cid,
                unit_name=entry.get("unit") or candidate.get("unit_name", "?"),
                branch=entry.get("branch") or candidate.get("branch", ""),
                status="landed",
                detail=f"committed to {draft['main_branch']}",
                merge_commit=new_head,
            )
        )
        if cleanup:
            _cleanup_unit(store, root, candidate)
    store.set_meta(DRAFT_META_KEY, None)
    gitutil.prune_worktrees(root)
    return results


def _abort_draft(
    store: Store, root: Path, config: dict[str, Any], draft: dict[str, Any]
) -> LandResult:
    wt_path = Path(draft["main_worktree"])
    gitutil.reset_hard(wt_path, draft["base_commit"])
    for entry in draft.get("candidates", []):
        cid = int(entry["id"])
        candidate = store.get_candidate(cid)
        if candidate is None:
            continue
        store.update_candidate(cid, status="prepared")
        unit = store.get_unit(int(candidate["unit_id"]))
        if unit:
            store.set_unit_state(int(unit["id"]), "working")
    store.set_meta(DRAFT_META_KEY, None)
    store.conn.commit()
    store.event("handoff.rejected", data={"base": draft["base_commit"]})
    gitutil.prune_worktrees(root)
    candidates = draft.get("candidates") or [{"id": 0, "unit": "?", "branch": ""}]
    first = candidates[0]
    return LandResult(
        candidate_id=int(first["id"]),
        unit_name=", ".join(c.get("unit", "?") for c in candidates),
        branch=first.get("branch", ""),
        status="rejected",
        detail="handoff discarded; main restored",
        draft=draft,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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
