"""Integration ordering for prepared candidates.

There is no runtime lease and no operation taxonomy. Candidates are merged in
the campaign DAG's wave order, a deterministic projection of ``dag.json``;
textual merge conflicts are still detected by git during ``integrate``.
``simulate`` materializes each wave's combined tree so the plane's trusted
checks can run once over the combined result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import campaign, gitutil
from .store import Store
from .util import IntergentError, scratch_dir
from .verifier import CheckResult, run_checks
from .waves import DEFAULT_WAVE_SIZE, plan_dag_waves


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
        node = str(candidate.get("unit_name") or "")
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
        node = str(candidate.get("unit_name") or "")
        index = wave_of.get(node, fallback)
        wave = by_index.get(index)
        if wave is None:
            wave = Wave(index=index)
            by_index[index] = wave
            waves.append(wave)
        wave.candidates.append(candidate)
    waves.sort(key=lambda w: w.index)
    return waves


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
        root, outcome.tree, [combined, gitutil.rev_parse(root, branch)], "intergent wave combine"
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
        except IntergentError:
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


__all__ = ["Wave", "plan_waves", "simulate"]
