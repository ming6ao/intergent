"""Campaign plane state: ``dag.json``, executor ``state.json`` and artifacts.

All campaign files live under ``.intergent/`` and are **prefixed by the
feature-branch name** so one campaign's files form a single glob and no two
campaigns collide (``docs/orchestration.md`` §4).  ``/`` in the branch name is
replaced with ``--``::

    feat/nanochat-cpp  ->  feat--nanochat-cpp

``dag.json`` is the canonical, machine-readable plan; it is plane state, never
committed to the repository.  ``state.json`` is a rebuildable cache of executor
progress (per-node status, last verdict, attempt counts) and git plus
``state.db`` always win over it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .util import IntergentError, read_json, state_dir, write_json

NODE_STATUSES = ("pending", "running", "done", "failed", "stopped")


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


def save_dag(root: Path, branch: str, dag: dict[str, Any]) -> Path:
    path = dag_path(root, branch)
    write_json(path, dag)
    return path


def load_state(root: Path, branch: str) -> dict[str, Any]:
    data = read_json(state_path(root, branch)) or {}
    nodes = data.get("nodes")
    if not isinstance(nodes, dict):
        data["nodes"] = {}
    data.setdefault("campaign", data.get("campaign"))
    return data


def save_state(root: Path, branch: str, state: dict[str, Any]) -> Path:
    path = state_path(root, branch)
    write_json(path, state)
    return path


def node_ids(dag: dict[str, Any]) -> list[str]:
    return [str(n["id"]) for n in dag.get("nodes", []) if n.get("id")]


def node_by_id(dag: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n["id"]): n for n in dag.get("nodes", []) if n.get("id")}


def node_status(state: dict[str, Any], node: str) -> str:
    entry = (state.get("nodes") or {}).get(node) or {}
    status = entry.get("status") if isinstance(entry, dict) else None
    return str(status or "pending")


def ready_nodes(
    dag: dict[str, Any],
    state: dict[str, Any],
    *,
    done: Iterable[str] | None = None,
) -> list[str]:
    """Nodes whose every dependency is ``done`` and which are not themselves done.

    ``ready(n) := every d in n.depends_on is done`` — there are no phases in the
    scheduler (``docs/orchestration.md`` §3).
    """
    done_set = set(done) if done is not None else {
        node for node in node_ids(dag) if node_status(state, node) == "done"
    }
    ready: list[str] = []
    for node in node_ids(dag):
        if node_status(state, node) in {"done", "running"}:
            continue
        deps = [str(d) for d in (node_by_id(dag)[node].get("depends_on") or [])]
        if all(dep in done_set for dep in deps):
            ready.append(node)
    return ready


def validate_dag(dag: dict[str, Any]) -> list[str]:
    """Return a list of human-readable plan problems (empty means valid)."""
    problems: list[str] = []
    ids = node_ids(dag)
    if not ids:
        problems.append("dag has no nodes")
    elif len(ids) != len(set(ids)):
        problems.append("dag node ids are not unique")
    known = set(ids)
    for raw in dag.get("nodes", []):
        node = str(raw.get("id"))
        for dep in raw.get("depends_on") or []:
            if str(dep) not in known:
                problems.append(f"node {node} depends on unknown node {dep}")
            if str(dep) == node:
                problems.append(f"node {node} depends on itself")
        if not raw.get("owns"):
            problems.append(f"node {node} owns no scopes")
        if not raw.get("acceptance"):
            problems.append(f"node {node} has no acceptance commands")
    if _has_cycle(dag):
        problems.append("dag has a dependency cycle")
    return problems


def _has_cycle(dag: dict[str, Any]) -> bool:
    graph = {
        str(n["id"]): [str(d) for d in (n.get("depends_on") or [])]
        for n in dag.get("nodes", [])
        if n.get("id")
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for dep in graph.get(node, []):
            if dep in graph and visit(dep):
                return True
        visiting.discard(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def load_campaign(root: Path, branch: str) -> dict[str, Any] | None:
    dag = load_dag(root, branch)
    if dag is None:
        return None
    state = load_state(root, branch)
    return {"dag": dag, "state": state, "ready": ready_nodes(dag, state)}


def summary(dag: dict[str, Any], state: dict[str, Any] | None = None) -> str:
    """Human-readable plan summary printed by ``campaign status``/``start``.

    Never written to disk (``docs/orchestration.md`` §3).
    """
    state = state or {}
    lines = [
        f"campaign: {dag.get('campaign', '(unnamed)')}",
        f"feature:  {dag.get('feature_branch', '(unset)')}  base: {dag.get('base', '(unset)')}",
        f"design:   {dag.get('design', '(unspecified)')}",
        f"nodes:    {len(node_ids(dag))}  concurrency: {dag.get('concurrency', '?')}",
    ]
    groups: dict[str, list[str]] = {}
    for raw in dag.get("nodes", []):
        groups.setdefault(str(raw.get("phase") or "-"), []).append(str(raw.get("id")))
    for phase, ids in groups.items():
        lines.append(f"  [{phase}] " + ", ".join(ids))
    if state:
        done = [n for n in node_ids(dag) if node_status(state, n) == "done"]
        failed = [n for n in node_ids(dag) if node_status(state, n) == "failed"]
        lines.append(f"  done: {len(done)}  failed: {len(failed)}")
    return "\n".join(lines)


def config_branch(config: dict[str, Any]) -> str:
    branch = config.get("main_branch")
    if not branch:
        raise IntergentError("plane has no main_branch; run `ig start` first")
    return str(branch)


__all__ = [
    "NODE_STATUSES",
    "branch_key",
    "config_branch",
    "dag_path",
    "load_campaign",
    "load_dag",
    "load_state",
    "node_by_id",
    "node_ids",
    "node_status",
    "ready_nodes",
    "report_path",
    "save_dag",
    "save_state",
    "state_path",
    "summary",
    "validate_dag",
    "worker_log_path",
]
