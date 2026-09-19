"""Scope canonicalization and hierarchy.

Implements section 2 of ``docs/conflict-engine.md``: deterministic,
language-aware normalization plus the scope tree used by the multi-granularity
lock manager in ``docs/local-plane.md`` (IS/IX/S/SIX/X).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from .util import IntergentError

KINDS = {
    "dir",
    "file",
    "symbol",
    "api",
    "schema",
    "config",
    "migration",
    "infra",
    "test",
    "unknown",
}

ADDITIVE = frozenset({"add", "extend", "modify"})
DESTRUCTIVE = frozenset({"replace", "remove", "rename", "migrate"})
OPERATIONS = ADDITIVE | DESTRUCTIVE

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN_RE = re.compile(r"[^a-zA-Z0-9]+")
_HTTP_RE = re.compile(r"^(get|post|put|patch|delete|head|options)\s+(.+)$", re.I)


@dataclass(frozen=True)
class Scope:
    kind: str
    key: str
    canonical: str
    source: str = "declared"

    @property
    def node(self) -> str:
        return f"{self.kind}:{self.canonical}"


def classify_operation(op: str) -> str:
    op = op.strip().lower()
    if op not in OPERATIONS:
        raise IntergentError(
            f"unknown operation '{op}'; expected one of {sorted(OPERATIONS)}"
        )
    return op


def is_destructive(op: str) -> bool:
    return op in DESTRUCTIVE


def infer_kind(key: str) -> str:
    key = key.strip()
    if not key:
        return "unknown"
    if "#" in key:
        return "symbol"
    if _HTTP_RE.match(key):
        return "api"
    if key.endswith("/"):
        return "dir"
    if "/" in key:
        return "file"
    if key.startswith(("migration", "migrate")):
        return "migration"
    return "unknown"


def parse_scope_spec(spec: str) -> tuple[str, str, str | None]:
    """Parse ``kind:key[=operation]``.

    Returns ``(kind, key, operation_or_None)``.  A trailing ``=op`` is only
    treated as an operation when the right-hand side names a real operation,
    so paths containing ``=`` survive intact.
    """
    text = spec.strip()
    operation: str | None = None
    if "=" in text:
        left, right = text.rsplit("=", 1)
        if right.strip().lower() in OPERATIONS:
            text, operation = left, right.strip().lower()
    if ":" in text:
        prefix, rest = text.split(":", 1)
        if prefix.strip().lower() in KINDS:
            return prefix.strip().lower(), rest.strip(), operation
    return infer_kind(text), text, operation


def _normalize_path(path: str) -> str:
    norm = posixpath.normpath(path.replace("\\", "/"))
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def canonical_key(kind: str, key: str) -> str:
    key = key.strip()
    if kind in {"file", "dir", "test"}:
        norm = _normalize_path(key) or "."
        if kind == "dir" and norm != "." and not norm.endswith("/"):
            norm = norm + "/"
        return norm.casefold()
    if kind == "symbol":
        path, _, sym = key.replace("\\", "/").partition("#")
        path_norm = _normalize_path(path) if path else ""
        return f"{path_norm.casefold()}#{sym.strip()}"
    if kind == "api":
        m = _HTTP_RE.match(key)
        if m:
            method = m.group(1).upper()
            path = re.sub(r"\s+", "", m.group(2))
            return f"{method} {path.casefold()}"
        return re.sub(r"\s+", " ", key).strip().casefold()
    if kind in {"schema", "config", "migration", "infra"}:
        return re.sub(r"\s+", "", key).casefold()
    return re.sub(r"\s+", " ", key).strip().casefold()


def make_scope(kind: str, key: str, source: str = "declared") -> Scope:
    kind = kind if kind in KINDS else infer_kind(key)
    return Scope(kind=kind, key=key.strip(), canonical=canonical_key(kind, key), source=source)


def tokens(kind: str, canonical: str) -> set[str]:
    """Token set used for the loose Jaccard match tier."""
    if kind == "symbol":
        _, _, sym = canonical.partition("#")
        parts = [sym]
    else:
        parts = [canonical]
    joined = " ".join(parts)
    pieces: list[str] = []
    for piece in _TOKEN_RE.split(joined):
        if piece:
            pieces.extend(_CAMEL_RE.split(piece))
    return {p.casefold() for p in pieces if p}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _dir_ancestors(path: str, *, include_self: bool) -> list[str]:
    """Return ``dir:`` nodes from shallowest to deepest for a path.

    ``include_self`` is True when the path names a directory (so the deepest
    node is the directory itself) and False when it names a file (so the file's
    own basename is not treated as a directory).
    """
    clean = path.strip("/")
    if not clean or clean == ".":
        return []
    parts = [p for p in clean.split("/") if p]
    if not include_self and parts:
        parts = parts[:-1]
    return ["dir:" + "/".join(parts[:i]) + "/" for i in range(1, len(parts) + 1)]


def scope_chain(kind: str, canonical: str) -> list[str]:
    """Ordered ``root -> ... -> node`` scope-tree chain for a canonical scope."""
    chain: list[str] = ["root:"]
    node = f"{kind}:{canonical}"
    if kind in {"file", "test"}:
        chain.extend(_dir_ancestors(canonical, include_self=False))
        chain.append(node)
    elif kind == "dir":
        chain.extend(_dir_ancestors(canonical, include_self=True))
        if not chain or chain[-1] != node:
            chain.append(node)
    elif kind == "symbol":
        path, _, sym = canonical.partition("#")
        chain.extend(_dir_ancestors(path, include_self=False))
        if path:
            chain.append(f"file:{path}")
        if sym:
            cls = sym.split(".")[0]
            if "." in sym and cls:
                chain.append(f"symbol:{path}#{cls}")
            chain.append(node)
    elif kind == "api":
        m = _HTTP_RE.match(canonical)
        if m:
            method = m.group(1).upper()
            path = m.group(2)
            segments = [s for s in path.split("/") if s]
            for i in range(1, len(segments)):
                chain.append("api:" + f"{method} " + "/".join(segments[:i]))
        chain.append(node)
    elif kind in {"schema", "config"}:
        sep = "." if "." in canonical else "/"
        if sep in canonical:
            parent = canonical.rsplit(sep, 1)[0]
            chain.append(f"{kind}:{parent}")
        chain.append(node)
    else:
        chain.append(node)

    # Deduplicate while preserving order (root first).
    seen: set[str] = set()
    ordered: list[str] = []
    for item in chain:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def parse_scope_specs(
    specs: list[str], default_operation: str
) -> list[tuple[Scope, str]]:
    """Parse a list of specs into ``(scope, operation)`` pairs."""
    default_operation = classify_operation(default_operation)
    result: list[tuple[Scope, str]] = []
    seen: set[tuple[str, str]] = set()
    for spec in specs:
        kind, key, op = parse_scope_spec(spec)
        scope = make_scope(kind, key)
        operation = classify_operation(op or default_operation)
        token = (scope.node, operation)
        if token in seen:
            continue
        seen.add(token)
        result.append((scope, operation))
    if not result:
        raise IntergentError("at least one scope is required (use --scope)")
    return result
