"""Plan-time directory ownership.

Ownership is by **directory subtree**. A campaign node declares the deepest
repo-relative directories that contain the paths it will touch; two nodes
conflict when their owned directories overlap by subtree (equal, ancestor, or
descendant). Files, symbols, APIs, and operations do not participate: the
planner is the single author of ownership, and the wave projection in
:mod:`intergent.waves` serializes overlapping nodes.

Canonical form:

* ``dir:src/api`` and ``src/api/`` both normalize to ``src/api``;
* the repository root is ``"."``;
* non-directory specs (``file:``, ``symbol:``, ...) are rejected, so a plan can
  never silently rely on finer-grained enforcement that no longer exists.
"""

from __future__ import annotations

import posixpath

from .util import IntergentError

#: Scope kinds that are *not* ownable. Kept explicit so a stale plan fails
#: loudly instead of being reinterpreted as a directory path.
_NON_DIR_KINDS = frozenset(
    {"file", "symbol", "api", "schema", "config", "migration", "infra", "test", "unknown"}
)


def normalize_dir(path: str) -> str:
    """Canonical repo-relative directory: ``dir:src/api/`` -> ``src/api``."""
    text = str(path).strip()
    if ":" in text:
        prefix, rest = text.split(":", 1)
        if prefix.strip().lower() == "dir":
            text = rest
    text = text.replace("\\", "/")
    norm = posixpath.normpath(text)
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.strip("/")
    return norm or "."


def parse_owns(specs: list[str]) -> list[str]:
    """Parse an ``owns`` list into normalized directories.

    Accepts ``dir:path`` and bare paths. A recognized non-directory kind raises
    :class:`IntergentError`.
    """
    owns: list[str] = []
    seen: set[str] = set()
    for spec in specs:
        text = str(spec).strip()
        if not text:
            continue
        if ":" in text:
            prefix = text.split(":", 1)[0].strip().lower()
            if prefix in _NON_DIR_KINDS:
                raise IntergentError(
                    f"owns must be directories, not {prefix}: '{text}' "
                    "(declare the deepest directory that contains the paths)"
                )
        directory = normalize_dir(text)
        if directory not in seen:
            seen.add(directory)
            owns.append(directory)
    return owns


def owns_conflict(a: list[str], b: list[str]) -> str | None:
    """Return a human reason when two owned directory sets overlap.

    Overlap is subtree overlap: equal directories, or one being an ancestor of
    the other. ``None`` means the two sets may run in the same wave.
    """
    dirs_a = {normalize_dir(x) for x in a}
    dirs_b = {normalize_dir(y) for y in b}
    for x in sorted(dirs_a):
        for y in sorted(dirs_b):
            if x == y:
                return f"directory conflict on {x}"
            if x == "." or y == ".":
                return f"directory conflict: root contains {y if x == '.' else x}"
            if x.startswith(y + "/"):
                return f"directory conflict: {y} contains {x}"
            if y.startswith(x + "/"):
                return f"directory conflict: {x} contains {y}"
    return None


def path_within_owns(path: str, owns: list[str]) -> bool:
    """True when *path* (a changed file) lives in one of the owned directories."""
    parent = posixpath.dirname(str(path).replace("\\", "/")).strip("/") or "."
    for owned in owns:
        directory = normalize_dir(owned)
        if directory == "." or parent == directory or parent.startswith(directory + "/"):
            return True
    return False


__all__ = ["normalize_dir", "owns_conflict", "parse_owns", "path_within_owns"]
