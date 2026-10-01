"""Sandbox profiles for the single verification executor.

Phase 1 ships the abstraction and the engine-side plumbing: a :class:`Sandbox`
describes how a check command is isolated, and its :meth:`Sandbox.digest` is
folded into the verification fingerprint so a stricter sandbox invalidates a
cached verdict.  Project-provided sandbox manifests and the coordinator gate
land in Phase 2 (``sliceme.sandbox.json`` + ``campaign start`` validation).

Resolution precedence is: an explicit override > the DAG's ``sandbox`` block >
the plane's ``policy.sandbox`` > ``none``.  ``none`` means "run unsandboxed" and
must be opt-in; the default is deliberately the least surprising one so a repo
without a sandbox recipe keeps working until the Phase 2 gate is in place.

The GPU broker is **sliceme's**, not the target project's: the executor composes
it (Phase 3).  A project may override the invocation through its sandbox
manifest.
"""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass, field
from typing import Any

from .util import SlicemeError, sha256_json

#: Recognised isolation modes.  ``none`` is unsandboxed and explicit.
SANDBOX_MODES = ("none", "bwrap", "unshare")

_BACKENDS = {"bwrap": "bwrap", "unshare": "unshare"}


@dataclass(frozen=True)
class Sandbox:
    """How a check command is isolated by the executor."""

    mode: str = "none"
    network: bool = True
    readonly_repo: bool = True
    writable: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "network": self.network,
            "readonly_repo": self.readonly_repo,
            "writable": list(self.writable),
        }

    def digest(self) -> str:
        """Hash only the isolation semantics, so a stricter sandbox re-verifies."""
        return sha256_json(
            {
                "mode": self.mode,
                "network": self.network,
                "readonly_repo": self.readonly_repo,
                "writable": sorted(self.writable),
            }
        )

    @property
    def offline(self) -> bool:
        return not self.network


def coerce_sandbox(raw: Any) -> Sandbox:
    """Normalise a manifest/config value into a :class:`Sandbox`."""
    if raw is None:
        return Sandbox()
    if isinstance(raw, Sandbox):
        return raw
    if isinstance(raw, str):
        if raw not in SANDBOX_MODES:
            raise SlicemeError(
                f"unknown sandbox mode: {raw!r} (want one of {', '.join(SANDBOX_MODES)})"
            )
        return Sandbox(mode=raw)
    if isinstance(raw, dict):
        mode = str(raw.get("mode") or "none")
        if mode not in SANDBOX_MODES:
            raise SlicemeError(
                f"unknown sandbox mode: {mode!r} (want one of {', '.join(SANDBOX_MODES)})"
            )
        writable = raw.get("writable") or ()
        if isinstance(writable, str):
            writable = (writable,)
        return Sandbox(
            mode=mode,
            network=bool(raw.get("network", True)),
            readonly_repo=bool(raw.get("readonly_repo", True)),
            writable=tuple(str(item) for item in writable),
        )
    raise SlicemeError(f"invalid sandbox specification: {raw!r}")


def resolve_sandbox(
    dag: dict[str, Any] | None,
    config: dict[str, Any] | None,
    *,
    override: str | None = None,
) -> Sandbox:
    """Resolve the effective sandbox for a campaign (see module docstring)."""
    if override:
        return coerce_sandbox(override)
    if dag and dag.get("sandbox") is not None:
        return coerce_sandbox(dag.get("sandbox"))
    policy = (config or {}).get("policy") or {}
    return coerce_sandbox(policy.get("sandbox"))


def backend_available(mode: str) -> bool:
    if mode == "none":
        return True
    binary = _BACKENDS.get(mode)
    return bool(binary) and shutil.which(binary) is not None


def wrap_command(command: str, sandbox: Sandbox, *, worktree: str | None = None) -> str:
    """Wrap a shell *command* in the sandbox's isolation prefix.

    ``none`` returns the command unchanged.  A requested backend that is not
    installed is a hard error (fail closed), never a silent downgrade.
    """
    if sandbox.mode == "none":
        return command
    if not backend_available(sandbox.mode):
        raise SlicemeError(
            f"sandbox mode '{sandbox.mode}' requested but '{_BACKENDS[sandbox.mode]}' "
            "is not installed"
        )
    if sandbox.mode == "bwrap":
        return _wrap_bwrap(command, sandbox, worktree)
    return _wrap_unshare(command, sandbox)


def _wrap_bwrap(command: str, sandbox: Sandbox, worktree: str | None) -> str:
    parts = ["bwrap", "--die-with-parent", "--unshare-pid"]
    if sandbox.offline:
        parts.append("--unshare-net")
    # The host is read-only unless the project explicitly allows writes, but the
    # scratch worktree stays writable so builds and test artifacts work.
    host_bind = "--ro-bind" if sandbox.readonly_repo else "--dev-bind"
    parts += [host_bind, "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    if worktree:
        parts += ["--bind", worktree, worktree]
    for path in sandbox.writable:
        target = path
        if not path.startswith("/") and worktree:
            target = f"{worktree.rstrip('/')}/{path}"
        parts += ["--bind", target, target]
    if worktree:
        parts += ["--chdir", worktree]
    parts += ["--", "/bin/sh", "-lc", command]
    return " ".join(shlex.quote(part) for part in parts)


def _wrap_unshare(command: str, sandbox: Sandbox) -> str:
    # ``unshare`` isolates namespaces but not the filesystem; the detached
    # scratch worktree remains the actual write barrier.  Projects that need a
    # stronger boundary provide their own command in the Phase 2 manifest.
    parts = ["unshare", "--mount", "--pid", "--fork", "--kill-child"]
    if sandbox.offline:
        parts.append("--net")
    parts += ["--", "/bin/sh", "-lc", command]
    return " ".join(shlex.quote(part) for part in parts)


__all__ = [
    "SANDBOX_MODES",
    "Sandbox",
    "backend_available",
    "coerce_sandbox",
    "resolve_sandbox",
    "wrap_command",
]
