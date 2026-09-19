"""Multi-granularity scope lock manager.

Implements the IS/IX/S/SIX/X compatibility matrix and root-to-leaf acquisition
rules from ``docs/local-plane.md``.  This module is pure (no database); the
service layer persists the computed requirements in ``claims``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .scopes import ADDITIVE, Scope, scope_chain

IS = "IS"
IX = "IX"
S = "S"
SIX = "SIX"
X = "X"

MODES = (IS, IX, S, SIX, X)

# Symmetric compatibility matrix (True = both may be granted simultaneously).
_ALLOWED = {
    (IS, IS),
    (IS, IX),
    (IS, S),
    (IS, SIX),
    (IX, IX),
    (S, S),
}
COMPAT: dict[tuple[str, str], bool] = {}
for _a in MODES:
    for _b in MODES:
        COMPAT[(_a, _b)] = (_a, _b) in _ALLOWED or (_b, _a) in _ALLOWED


def compatible(a: str, b: str) -> bool:
    return COMPAT.get((a, b), False)


def combine(modes: set[str]) -> str:
    if X in modes:
        return X
    has_s = S in modes
    has_ix = IX in modes
    if has_s and has_ix:
        return SIX
    if has_s:
        return S
    if has_ix:
        return IX
    return IS


@dataclass(frozen=True)
class Requirement:
    node: str
    mode: str
    declared: bool  # True if this node is a declared scope (leaf), else ancestor


def requirement_closure(items: list[tuple[Scope, str]]) -> dict[str, Requirement]:
    """Map every scope-tree node touched by an intent to its lock mode."""
    raw: dict[str, set[str]] = {}
    declared: set[str] = set()
    for scope, op in items:
        chain = scope_chain(scope.kind, scope.canonical)
        node = chain[-1]
        mode = S if op in ADDITIVE else X
        raw.setdefault(node, set()).add(mode)
        declared.add(node)
        intent_mode = IS if mode == S else IX
        for ancestor in chain[:-1]:
            raw.setdefault(ancestor, set()).add(intent_mode)
    return {
        node: Requirement(node=node, mode=combine(modes), declared=node in declared)
        for node, modes in raw.items()
    }


@dataclass(frozen=True)
class HeldLock:
    unit_id: int
    unit_name: str
    node: str
    mode: str


def find_blocker(
    requirements: dict[str, Requirement], held: list[HeldLock]
) -> HeldLock | None:
    """Return the first held lock incompatible with the requested closure."""
    for node, req in requirements.items():
        for lock in held:
            if lock.node == node and not compatible(req.mode, lock.mode):
                return lock
    # A held lock on a node that is an ancestor of a requested node is always
    # represented, because requirements include every ancestor.  A held lock on
    # a descendant likewise contributes an intention lock on the requested node.
    return None


def describe(requirements: dict[str, Requirement]) -> str:
    leaves = [f"{r.node}={r.mode}" for r in requirements.values() if r.declared]
    return ", ".join(sorted(leaves))
