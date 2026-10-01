"""Campaign plane state paths and DAG/state readers.

All campaign files live under ``.intergent/`` and are **prefixed by the
feature-branch name** so one campaign's files form a single glob and no two
campaigns collide (``docs/orchestration.md`` §4).  ``/`` in the branch name is
replaced with ``--``::

    feat/nanochat-cpp  ->  feat--nanochat-cpp

The orchestrator (the pi `campaign` extension) owns writing ``dag.json`` and
``state.json``; Python only reads them for ``ig report`` and resolves their
paths.  ``dag.json`` is plane state, never committed to the repository.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import IntergentError, read_json, state_dir


def branch_key(branch: str) -> str:
    """The file prefix for a feature branch (``feat/x`` -> ``feat--x``)."""
    key = (branch or "main").strip().replace("/", "--")
    return key or "main"


def dag_path(root: Path, branch: str) -> Path:
    return state_dir(root) / f"{branch_key(branch)}.dag.json"


def state_path(root: Path, branch: str) -> Path:
    return state_dir(root) / f"{branch_key(branch)}.state.json"


def report_path(root: Path, branch: str) -> Path:
    return state_dir(root) / f"{branch_key(branch)}.report.md"


def worker_log_path(root: Path, branch: str, node: str) -> Path:
    return state_dir(root) / f"{branch_key(branch)}.worker_{node}.log"


def load_dag(root: Path, branch: str) -> dict[str, Any] | None:
    return read_json(dag_path(root, branch))


def load_state(root: Path, branch: str) -> dict[str, Any]:
    data = read_json(state_path(root, branch)) or {}
    if not isinstance(data.get("nodes"), dict):
        data["nodes"] = {}
    return data


def node_by_id(dag: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n["id"]): n for n in dag.get("nodes", []) if n.get("id")}


def node_status(state: dict[str, Any], node: str) -> str:
    entry = (state.get("nodes") or {}).get(node) or {}
    status = entry.get("status") if isinstance(entry, dict) else None
    return str(status or "pending")


def config_branch(config: dict[str, Any]) -> str:
    branch = config.get("main_branch")
    if not branch:
        raise IntergentError("plane has no main_branch; run `ig start` first")
    return str(branch)


__all__ = [
    "branch_key",
    "config_branch",
    "dag_path",
    "load_dag",
    "load_state",
    "node_by_id",
    "node_status",
    "report_path",
    "state_path",
    "worker_log_path",
]
