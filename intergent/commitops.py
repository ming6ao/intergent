"""Shared campaign integration helpers.

Small, git/store-facing primitives used by :mod:`intergent.integrate` (and the
wave planner) to land a verified candidate on the campaign feature branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitutil
from .planner import plan_waves
from .store import Store
from .util import worktrees_dir
from .verifier import CheckResult


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


def mark_landed(store: Store, candidate: dict[str, Any], merge_commit: str) -> None:
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


__all__ = [
    "LandResult",
    "checks_output",
    "conflict_summary",
    "main_branch_of",
    "main_worktree",
    "mark_landed",
    "ordered_candidates",
]
