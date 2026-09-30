"""Deterministic campaign report skeleton (``docs/orchestration.md`` §6.3).

``ig report`` writes ``.intergent/<branch-key>.report.md``: a deterministic
skeleton (design ref, feature branch, nodes, worker ids, commits,
fingerprints/verifications, artifact paths) with an optional narrative section
appended by the coordinator.  The skeleton is stable across runs for unchanged
state so it can be diffed and regenerated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import campaign
from .store import Store


def _verification_line(verification: dict[str, Any]) -> str:
    source = verification.get("source") or "plane"
    status = verification.get("status")
    gpu = verification.get("gpu") or "-"
    fingerprint = (verification.get("fingerprint") or "")[:12]
    return f"{source}: {status} (fp {fingerprint}, gpu {gpu})"


def build_skeleton(
    root: Path,
    config: dict[str, Any],
    store: Store,
    *,
    branch: str | None = None,
    design: str | None = None,
) -> dict[str, Any]:
    branch = branch or campaign.config_branch(config)
    dag = campaign.load_dag(root, branch)
    state = campaign.load_state(root, branch)
    nodes = campaign.node_by_id(dag) if dag else {}

    units = sorted(store.list_units(), key=lambda u: int(u["id"]))
    candidates = sorted(store.list_candidates(), key=lambda c: int(c["id"]))

    per_node: list[dict[str, Any]] = []
    for unit in units:
        name = str(unit["name"])
        node = nodes.get(name)
        unit_candidates = [c for c in candidates if int(c["unit_id"]) == int(unit["id"])]
        latest = unit_candidates[-1] if unit_candidates else None
        verification = (
            store.latest_verification(int(latest["id"])) if latest is not None else None
        )
        per_node.append(
            {
                "node": name,
                "label": (node or {}).get("label"),
                "phase": (node or {}).get("phase"),
                "status": campaign.node_status(state, name) if dag else unit["state"],
                "unit_state": unit["state"],
                "branch": unit["branch"],
                "log": str(campaign.worker_log_path(root, branch, name)),
                "candidate": int(latest["id"]) if latest is not None else None,
                "candidate_status": latest["status"] if latest is not None else None,
                "commit": latest["head_commit"] if latest is not None else None,
                "verification": verification,
            }
        )

    return {
        "campaign": (dag or {}).get("campaign") or config.get("campaign", {}).get("name"),
        "design": design or (dag or {}).get("design") or config.get("campaign", {}).get("design"),
        "feature_branch": branch,
        "base": (dag or {}).get("base") or config.get("base"),
        "concurrency": (dag or {}).get("concurrency"),
        "artifact_paths": {
            "dag": str(campaign.dag_path(root, branch)),
            "state": str(campaign.state_path(root, branch)),
            "report": str(campaign.report_path(root, branch)),
        },
        "nodes": per_node,
    }


def render(skeleton: dict[str, Any], narrative: str | None = None) -> str:
    lines: list[str] = []
    lines.append("# Campaign report: " + str(skeleton.get("campaign") or "(unnamed)"))
    lines.append("")
    lines.append(f"- Design: {skeleton.get('design') or '(unspecified)'}")
    lines.append(f"- Feature branch: {skeleton.get('feature_branch')}")
    lines.append(f"- Base: {skeleton.get('base') or '(unspecified)'}")
    if skeleton.get("concurrency") is not None:
        lines.append(f"- Concurrency: {skeleton['concurrency']}")
    lines.append("")
    lines.append("## Nodes")
    lines.append("")
    lines.append("| node | phase | status | unit | branch | candidate | commit | verification |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for node in skeleton.get("nodes", []):
        verification = node.get("verification") or {}
        verification_text = (
            f"{verification.get('source') or 'plane'}:{verification.get('status')}"
            if verification
            else "-"
        )
        commit = node.get("commit") or "-"
        lines.append(
            "| {node} | {phase} | {status} | {unit} | {branch} | {candidate} | {commit} | {verification} |".format(
                node=node["node"],
                phase=node.get("phase") or "-",
                status=node.get("status"),
                unit=node.get("unit_state"),
                branch=node.get("branch"),
                candidate=node.get("candidate") or "-",
                commit=commit[:12],
                verification=verification_text,
            )
        )
    lines.append("")
    lines.append("## Verifications")
    lines.append("")
    for node in skeleton.get("nodes", []):
        verification = node.get("verification")
        if verification:
            lines.append(f"- {node['node']}: " + _verification_line(verification))
    lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    for name, path in sorted((skeleton.get("artifact_paths") or {}).items()):
        lines.append(f"- {name}: {path}")
    lines.append("")
    lines.append("## What changed / risks")
    lines.append("")
    lines.append(narrative.strip() if narrative and narrative.strip() else "(no narrative supplied)")
    lines.append("")
    return "\n".join(lines)


def write_report(
    root: Path,
    config: dict[str, Any],
    store: Store,
    *,
    narrative: str | None = None,
    branch: str | None = None,
    design: str | None = None,
) -> dict[str, Any]:
    branch = branch or campaign.config_branch(config)
    skeleton = build_skeleton(root, config, store, branch=branch, design=design)
    content = render(skeleton, narrative)
    path = campaign.report_path(root, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "content": content, "skeleton": skeleton}


__all__ = ["build_skeleton", "render", "write_report"]
