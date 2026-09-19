"""Git plumbing.

Mutating operations shell out to the system ``git`` (worktree, merge, rebase,
merge-tree, update-ref) exactly as prescribed by ``docs/operations.md``.  The
module is deliberately small and side-effect free apart from the named verbs.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .util import IntergentError


@dataclass
class GitResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def git(
    repo: str | os.PathLike[str],
    *args: str,
    check: bool = False,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> GitResult:
    cmd = ["git", "-C", str(repo), *args]
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        env=full_env,
    )
    result = GitResult(list(args), proc.returncode, proc.stdout, proc.stderr)
    if check and not result.ok:
        raise IntergentError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def require_git() -> None:
    try:
        proc = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:  # pragma: no cover
        raise IntergentError("git is required but was not found on PATH") from exc
    if proc.returncode != 0:
        raise IntergentError("git is required but does not run")


def is_git_repo(path: str | os.PathLike[str]) -> bool:
    return git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def toplevel(path: str | os.PathLike[str]) -> Path:
    res = git(path, "rev-parse", "--show-toplevel", check=True)
    return Path(res.stdout.strip()).resolve()


def current_branch(path: str | os.PathLike[str]) -> str:
    res = git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if res.ok:
        return res.stdout.strip()
    return ""


def rev_parse(repo: str | os.PathLike[str], ref: str) -> str:
    res = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if not res.ok:
        res = git(repo, "rev-parse", "--verify", ref, check=False)
    if not res.ok:
        raise IntergentError(f"cannot resolve git ref: {ref}")
    return res.stdout.strip()


def branch_exists(repo: str | os.PathLike[str], branch: str) -> bool:
    return git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").ok


def tree_of(repo: str | os.PathLike[str], commit: str) -> str:
    res = git(repo, "rev-parse", f"{commit}^{{tree}}", check=True)
    return res.stdout.strip()


def status_porcelain(worktree: str | os.PathLike[str]) -> list[str]:
    res = git(worktree, "status", "--porcelain", check=True)
    return [line for line in res.stdout.splitlines() if line.strip()]


def is_clean(worktree: str | os.PathLike[str]) -> bool:
    return not status_porcelain(worktree)


def head_commit(worktree: str | os.PathLike[str]) -> str:
    return git(worktree, "rev-parse", "HEAD", check=True).stdout.strip()


@dataclass
class WorktreeEntry:
    path: Path
    head: str
    branch: str
    detached: bool


def list_worktrees(repo: str | os.PathLike[str]) -> list[WorktreeEntry]:
    res = git(repo, "worktree", "list", "--porcelain", check=True)
    entries: list[WorktreeEntry] = []
    current: dict[str, str] = {}
    for raw in res.stdout.splitlines():
        line = raw.strip()
        if not line:
            if current:
                entries.append(_entry(current))
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree ") :]
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :].removeprefix("refs/heads/")
        elif line == "detached":
            current["detached"] = "1"
    if current:
        entries.append(_entry(current))
    return entries


def _entry(data: dict[str, str]) -> WorktreeEntry:
    return WorktreeEntry(
        path=Path(data.get("path", "")),
        head=data.get("head", ""),
        branch=data.get("branch", ""),
        detached="detached" in data,
    )


def worktree_for_branch(repo: str | os.PathLike[str], branch: str) -> WorktreeEntry | None:
    for entry in list_worktrees(repo):
        if entry.branch == branch:
            return entry
    return None


def add_worktree(
    repo: str | os.PathLike[str],
    path: Path,
    *,
    branch: str,
    base: str,
    new_branch: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if new_branch:
        git(repo, "worktree", "add", "-b", branch, str(path), base, check=True)
    else:
        git(repo, "worktree", "add", str(path), branch, check=True)


def add_detached_worktree(repo: str | os.PathLike[str], path: Path, commit: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--detach", str(path), commit, check=True)


def remove_worktree(repo: str | os.PathLike[str], path: Path, *, force: bool = True) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    git(repo, *args, check=False)


def prune_worktrees(repo: str | os.PathLike[str]) -> None:
    git(repo, "worktree", "prune", check=False)


def delete_branch(repo: str | os.PathLike[str], branch: str, *, force: bool = True) -> None:
    flag = "-D" if force else "-d"
    git(repo, "branch", flag, branch, check=False)


@dataclass
class MergeOutcome:
    clean: bool
    tree: str | None
    conflicts: list[str]
    raw: str


def merge_tree(
    repo: str | os.PathLike[str], lhs: str, rhs: str
) -> MergeOutcome:
    """Test-merging two commits/branches without touching a worktree."""
    res = git(repo, "merge-tree", "--write-tree", lhs, rhs, check=False)
    if res.returncode == 0:
        tree = res.stdout.splitlines()[0].strip() if res.stdout.strip() else None
        return MergeOutcome(True, tree, [], res.stdout)
    conflicts = _parse_merge_tree_conflicts(res.stdout)
    return MergeOutcome(False, None, conflicts, res.stdout + res.stderr)


_CONFLICT_RE = re.compile(r"^CONFLICT \(([^)]+)\): (.*)$")


def _parse_merge_tree_conflicts(output: str) -> list[str]:
    found: list[str] = []
    for line in output.splitlines():
        m = _CONFLICT_RE.match(line.strip())
        if m:
            found.append(f"{m.group(1)}: {m.group(2)}")
    return found


def merge_base(repo: str | os.PathLike[str], a: str, b: str) -> str:
    res = git(repo, "merge-base", a, b, check=False)
    return res.stdout.strip()


def merge_into(
    worktree: str | os.PathLike[str],
    branch: str,
    *,
    message: str,
    no_ff: bool = True,
    ff_only: bool = False,
) -> GitResult:
    args = ["merge", "--no-edit"]
    if no_ff:
        args.append("--no-ff")
    if ff_only:
        args.append("--ff-only")
    args += ["-m", message, branch]
    return git(worktree, *args, check=False)


def merge_abort(worktree: str | os.PathLike[str]) -> None:
    git(worktree, "merge", "--abort", check=False)


def commit_all(
    worktree: str | os.PathLike[str], message: str, *, allow_empty: bool = False
) -> GitResult:
    git(worktree, "add", "-A", check=True)
    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    return git(worktree, *args, check=False)


def rebase_onto(
    worktree: str | os.PathLike[str], upstream: str
) -> GitResult:
    return git(worktree, "rebase", upstream, check=False)


def log_subjects(repo: str | os.PathLike[str], base: str, head: str) -> list[str]:
    res = git(repo, "log", "--format=%h %s", f"{base}..{head}", check=False)
    return res.stdout.splitlines()


def diff_names(repo: str | os.PathLike[str], base: str, head: str) -> list[str]:
    res = git(repo, "diff", "--name-only", f"{base}...{head}", check=False)
    return [line for line in res.stdout.splitlines() if line.strip()]


def ahead_behind(repo: str | os.PathLike[str], base: str, head: str) -> tuple[int, int]:
    res = git(repo, "rev-list", "--left-right", "--count", f"{base}...{head}", check=False)
    if not res.ok or not res.stdout.strip():
        return (0, 0)
    left, right = res.stdout.split()
    return int(left), int(right)


def detect_toolchain_files(repo: str | os.PathLike[str], commit: str) -> dict[str, str]:
    """Hash lockfiles that pin the toolchain, so verification caches invalidate."""
    candidates = [
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "go.sum",
        "poetry.lock",
        "requirements.txt",
        "Gemfile.lock",
        "composer.lock",
    ]
    digests: dict[str, str] = {}
    for name in candidates:
        res = git(repo, "show", f"{commit}:{name}", check=False)
        if res.ok:
            from .util import sha256_text

            digests[name] = sha256_text(res.stdout)
    return digests


def update_ref(
    repo: str | os.PathLike[str],
    ref: str,
    new: str,
    *,
    old: str | None = None,
    message: str | None = None,
) -> None:
    args = ["update-ref"]
    if message:
        args += ["-m", message]
    args.append(ref)
    args.append(new)
    if old:
        args.append(old)
    git(repo, *args, check=True)


def create_branch(repo: str | os.PathLike[str], branch: str, commit: str) -> None:
    git(repo, "branch", "-f", branch, commit, check=True)


def stash_or_fail(worktree: str | os.PathLike[str]) -> None:
    if not is_clean(worktree):
        raise IntergentError(
            f"worktree {worktree} has uncommitted changes; commit or discard them first"
        )


def short(repo: str | os.PathLike[str], ref: str) -> str:
    return git(repo, "rev-parse", "--short", ref, check=False).stdout.strip()
