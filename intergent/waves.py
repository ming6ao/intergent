"""Deterministic wave planning over the campaign DAG.

This is the **scheduler** projection of the plan.  The DAG in
``.intergent/<branch-key>.dag.json`` remains the only authored schedule; waves
are computed from it, never hand-written.  A wave is a maximal set of nodes that

* may run concurrently (each in its own worktree), and
* do not conflict on any owned directory.

Ownership is by directory subtree (:mod:`intergent.scopes`): two nodes
conflict when one owned directory is equal to, an ancestor of, or a descendant
of the other's.  This is deliberately strict, and it is the *only* runtime
serialization mechanism: there is no lease system.

Waves obey the DAG's ``depends_on`` edges: a node is never placed earlier than
``max(wave(dep) + 1)``, so every dependency is integrated before its dependents
start.  The per-wave size is capped by ``concurrency`` (default 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .scopes import owns_conflict, parse_owns
from .util import IntergentError

DEFAULT_WAVE_SIZE = 3


@dataclass
class DagWave:
    index: int
    members: list[str] = field(default_factory=list)
    conflicts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wave": self.index,
            "members": list(self.members),
            "conflicts": dict(self.conflicts),
        }


def _node_id(node: dict[str, Any]) -> str:
    nid = node.get("id")
    if not nid:
        raise IntergentError("dag node is missing an id")
    return str(nid)


def node_owns(node: dict[str, Any]) -> list[str]:
    """Parse a node's ``owns`` into normalized directories (validates kinds)."""
    return parse_owns(node.get("owns") or [])


def _conflict_reason(a: dict[str, Any], b: dict[str, Any]) -> str | None:
    """Return a human reason when nodes *a* and *b* must not share a wave.

    When *a* is compared against *b*, *b* is already placed and *a* is the
    candidate moving later, so the message names the blocker (*b*).
    """
    owns_a = node_owns(a)
    if not owns_a:
        return None
    reason = owns_conflict(owns_a, node_owns(b))
    if reason:
        return f"{reason} with '{_node_id(b)}'"
    return None


def _topological_order(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Kahn topological sort, stable by declaration order.

    Raises :class:`IntergentError` on duplicate ids, unknown dependencies, or a
    dependency cycle.
    """
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for node in nodes:
        nid = _node_id(node)
        if nid in by_id:
            raise IntergentError(f"dag node id '{nid}' appears more than once")
        by_id[nid] = node
        order.append(nid)

    indegree: dict[str, int] = {nid: 0 for nid in order}
    dependents: dict[str, list[str]] = {nid: [] for nid in order}
    for nid in order:
        node = by_id[nid]
        for dep in node.get("depends_on") or []:
            dep = str(dep)
            if dep not in by_id:
                raise IntergentError(f"dag node '{nid}' depends on unknown node '{dep}'")
            indegree[nid] += 1
            dependents[dep].append(nid)

    # Always take the earliest-declared zero-indegree node so placement is
    # deterministic and independent of dict/set iteration order.
    placed: set[str] = set()
    sorted_ids: list[str] = []
    while len(sorted_ids) < len(order):
        picked = next(
            (nid for nid in order if nid not in placed and indegree[nid] == 0), None
        )
        if picked is None:
            remaining = [nid for nid in order if nid not in placed]
            raise IntergentError(
                f"dependency cycle among dag nodes: {', '.join(remaining)}"
            )
        placed.add(picked)
        sorted_ids.append(picked)
        for dependent in dependents[picked]:
            indegree[dependent] -= 1

    return [by_id[nid] for nid in sorted_ids]


def validate_dag(nodes: list[dict[str, Any]]) -> None:
    """Validate a DAG's ids, edges, acyclicity, and directory-only ``owns``."""
    ordered = _topological_order(nodes)  # raises on duplicates/unknown/cycle
    for node in ordered:
        node_owns(node)  # raises on a non-directory owns spec


def plan_dag_waves(
    nodes: list[dict[str, Any]],
    *,
    wave_size: int = DEFAULT_WAVE_SIZE,
) -> list[DagWave]:
    """Pack DAG *nodes* into ordered waves.

    ``wave_size`` is the maximum number of nodes per wave (the campaign's
    ``concurrency``, default 3).  A node is placed in the earliest wave that

    * is at least ``max(wave(dep) + 1)`` for every dependency,
    * has room under ``wave_size``, and
    * contains no node whose owned directories overlap (strict subtree rule).
    """
    if wave_size < 1:
        raise IntergentError("wave_size (concurrency) must be >= 1")

    waves: list[DagWave] = []
    wave_of: dict[str, int] = {}
    by_id = {_node_id(n): n for n in nodes}

    for node in _topological_order(nodes):
        nid = _node_id(node)
        node_owns(node)  # validate directory-only owns, even for lone nodes
        min_wave = 0
        for dep in node.get("depends_on") or []:
            min_wave = max(min_wave, wave_of[str(dep)] + 1)

        blocked_reason: str | None = None
        placed = False
        for wave in waves:
            if wave.index < min_wave:
                continue
            if len(wave.members) >= wave_size:
                continue
            reason: str | None = None
            for member_id in wave.members:
                reason = _conflict_reason(node, by_id[member_id])
                if reason:
                    break
            if reason:
                blocked_reason = reason
                continue
            wave.members.append(nid)
            wave_of[nid] = wave.index
            placed = True
            break

        if not placed:
            wave = DagWave(index=len(waves))
            wave.members.append(nid)
            if blocked_reason:
                wave.conflicts[nid] = blocked_reason
            waves.append(wave)
            wave_of[nid] = wave.index

    return waves


__all__ = ["DEFAULT_WAVE_SIZE", "DagWave", "node_owns", "plan_dag_waves", "validate_dag"]
