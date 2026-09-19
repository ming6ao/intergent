"""Local verification pinned to content fingerprints.

Runner executes configured trusted checks in a clean scratch worktree checked
out at the candidate commit.  The result is pinned to a fingerprint over
``(tree, command vector, toolchain, policy)`` so any change invalidates it
(``docs/local-plane.md`` section 4).
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitutil
from .util import IntergentError, scratch_dir, sha256_json, sha256_text


@dataclass
class CheckSpec:
    name: str
    command: str
    required: bool = True
    timeout: int = 900

    @classmethod
    def from_config(cls, item: Any) -> "CheckSpec":
        if isinstance(item, str):
            return cls(name=item, command=item)
        if isinstance(item, dict):
            return cls(
                name=str(item.get("name") or item.get("command")),
                command=str(item["command"]),
                required=bool(item.get("required", True)),
                timeout=int(item.get("timeout", 900)),
            )
        raise IntergentError(f"invalid check entry: {item!r}")


@dataclass
class Fingerprint:
    fingerprint: str
    tree: str
    cmd_digest: str
    toolchain_digest: str
    policy_digest: str


@dataclass
class CheckResult:
    name: str
    command: str
    status: str
    returncode: int
    output: str
    duration: float
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "status": self.status,
            "returncode": self.returncode,
            "duration": round(self.duration, 3),
            "required": self.required,
            "output": self.output[-4000:],
        }


@dataclass
class VerificationResult:
    status: str  # passed | failed | error
    fingerprint: Fingerprint
    checks: list[CheckResult] = field(default_factory=list)
    from_cache: bool = False
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "fingerprint": self.fingerprint.fingerprint,
            "from_cache": self.from_cache,
            "duration": round(self.duration, 3),
            "checks": [c.to_dict() for c in self.checks],
        }


def checks_from_config(config: dict[str, Any]) -> list[CheckSpec]:
    raw = config.get("checks") or []
    return [CheckSpec.from_config(item) for item in raw]


def toolchain_digest(root: Path, commit: str) -> str:
    files = gitutil.detect_toolchain_files(root, commit)
    git_version = gitutil.git(root, "--version").stdout.strip()
    return sha256_json({"git": git_version, "lockfiles": files, "python": _py_version()})


def _py_version() -> str:
    import sys

    return sys.version.split()[0]


def compute_fingerprint(
    root: Path, config: dict[str, Any], commit: str
) -> Fingerprint:
    tree = gitutil.tree_of(root, commit)
    checks = checks_from_config(config)
    cmd_digest = sha256_json(
        [
            {"name": c.name, "command": c.command, "required": c.required, "timeout": c.timeout}
            for c in checks
        ]
    )
    tool = toolchain_digest(root, commit)
    policy = config.get("policy") or {}
    policy_digest = sha256_json(policy)
    fingerprint = sha256_text("\n".join([tree, cmd_digest, tool, policy_digest]))
    return Fingerprint(fingerprint, tree, cmd_digest, tool, policy_digest)


def run_checks(
    root: Path,
    config: dict[str, Any],
    commit: str,
    *,
    worktree: Path | None = None,
    only: list[str] | None = None,
) -> tuple[str, list[CheckResult], float]:
    checks = checks_from_config(config)
    if only:
        wanted = {name.casefold() for name in only}
        checks = [c for c in checks if c.name.casefold() in wanted]
    tmp_created = False
    if worktree is None:
        worktree = scratch_dir(root) / f"verify-{os.getpid()}-{int(time.time() * 1000)}"
        gitutil.add_detached_worktree(root, worktree, commit)
        tmp_created = True
    results: list[CheckResult] = []
    started = time.time()
    try:
        for check in checks:
            results.append(_run_one(worktree, check))
    finally:
        if tmp_created:
            _cleanup_worktree(root, worktree)
    duration = time.time() - started
    if not checks:
        return "passed", results, duration
    failed_required = any(r.status != "passed" and r.required for r in results)
    errored = any(r.status == "error" for r in results)
    if failed_required:
        return ("error" if errored else "failed"), results, duration
    return "passed", results, duration


def _run_one(worktree: Path, check: CheckSpec) -> CheckResult:
    started = time.time()
    try:
        proc = subprocess.run(
            check.command,
            cwd=str(worktree),
            shell=True,
            capture_output=True,
            text=True,
            timeout=check.timeout,
        )
        duration = time.time() - started
        status = "passed" if proc.returncode == 0 else "failed"
        output = _combine_output(proc.stdout, proc.stderr)
        return CheckResult(check.name, check.command, status, proc.returncode, output, duration, check.required)
    except subprocess.TimeoutExpired as exc:
        duration = time.time() - started
        output = _combine_output(
            exc.stdout if isinstance(exc.stdout, str) else "",
            f"timed out after {check.timeout}s",
        )
        return CheckResult(check.name, check.command, "error", 124, output, duration, check.required)
    except OSError as exc:
        duration = time.time() - started
        return CheckResult(check.name, check.command, "error", 127, str(exc), duration, check.required)


def _combine_output(stdout: str | None, stderr: str | None) -> str:
    parts = []
    if stdout and stdout.strip():
        parts.append(stdout.strip())
    if stderr and stderr.strip():
        parts.append("[stderr]\n" + stderr.strip())
    return "\n".join(parts)


def _cleanup_worktree(root: Path, worktree: Path) -> None:
    gitutil.remove_worktree(root, worktree, force=True)
    from .util import rmtree

    rmtree(worktree)
    gitutil.prune_worktrees(root)
