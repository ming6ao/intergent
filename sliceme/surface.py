"""The one action surface, shared by every adapter.

The CLI and the pi extension both derive their verbs/tools from
:data:`ACTIONS`, so a surface can never exist in one adapter and not another.
:func:`dispatch` is the single implementation every adapter calls; adapters only
parse arguments and render results.

Names are deliberately few and overloaded by flags.  Every action is
agent-callable; there are no human-only actions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .util import SlicemeError

if TYPE_CHECKING:  # pragma: no cover
    from .service import Service


@dataclass(frozen=True)
class Param:
    """One argument, rendered as a CLI flag and a tool property."""

    name: str
    type: str  # string | boolean | int | list
    help: str
    required: bool = False
    choices: tuple[str, ...] = ()
    flag: str | None = None  # CLI flag/stem; defaults to name with dashes


@dataclass(frozen=True)
class Action:
    name: str
    summary: str
    params: tuple[Param, ...] = ()
    aliases: tuple[str, ...] = ()


ACTIONS: tuple[Action, ...] = (
    Action(
        name="start",
        summary="bootstrap the plane and a unit for this directory (idempotent)",
        aliases=("init",),
        params=(
            Param("name", "string", "unit name (default: slug of the directory name)"),
            Param("path", "string", "directory to bootstrap (default: cwd)"),
            Param("session", "string", "session name (default: unit name)"),
            Param("kind", "string", "unit kind", choices=("session", "worker")),
            Param("base", "string", "base branch/ref for new worktrees"),
            Param("task", "string", "task description stored on the session"),
            Param("main_branch", "string", "integration branch to adopt (default: current; must exist)", flag="main"),
            Param("checks", "list", "trusted check NAME=COMMAND (repeatable)", flag="check"),
            Param("force", "boolean", "overwrite an existing config"),
            Param("no_unit", "boolean", "initialise the plane without creating a unit for cwd"),
        ),
    ),
    Action(
        name="status",
        summary="show units, candidates, waves, and health",
        params=(
            Param("unit", "string", "show one unit instead of the summary"),
            Param("short", "boolean", "print only the current unit name"),
            Param("simulate", "boolean", "plan waves and verify the combined tree"),
            Param("health", "boolean", "check git/plane health"),
            Param("gc", "boolean", "prune worktrees and landed-unit branches"),
            Param("no_checks", "boolean", "with --simulate: plan only, do not run checks"),
        ),
    ),
    Action(
        name="commit",
        summary="commit the worktree and register the candidate",
        params=(
            Param("unit", "string", "unit (defaults to the worktree containing cwd)"),
            Param("message", "string", "commit message", required=True),
            Param("summary", "string", "candidate summary for review"),
        ),
    ),
    Action(
        name="integrate",
        summary="merge verified candidates onto the campaign feature branch",
        params=(
            Param("node", "string", "only the candidate for this node/unit id"),
            Param("cleanup", "string", "cleanup after integrating", choices=("none", "worktrees", "all")),
            Param("acceptance", "list", "node acceptance command (repeatable)", flag="acceptance"),
            Param("gpu", "string", "GPU tier reserved by the verifier", choices=("none", "T1", "T2")),
            Param("check_only", "boolean", "record the verdict without merging (orchestrator verify)"),
            Param("no_checks", "boolean", "skip the plane's trusted checks"),
        ),
    ),
    Action(
        name="report",
        summary="write the deterministic campaign report skeleton (plus optional narrative)",
        params=(
            Param("narrative", "string", "what-changed/risks text appended to the skeleton"),
            Param("design", "string", "design document reference (default: dag.json)"),
        ),
    ),
)

ACTION_BY_NAME: dict[str, Action] = {a.name: a for a in ACTIONS}
_ALIAS_TO_NAME: dict[str, str] = {alias: a.name for a in ACTIONS for alias in a.aliases}


def resolve_action(name: str) -> Action:
    canonical = _ALIAS_TO_NAME.get(name, name)
    action = ACTION_BY_NAME.get(canonical)
    if action is None:
        raise SlicemeError(f"unknown action: {name}")
    return action


def all_params() -> tuple[Param, ...]:
    """Union of parameters across actions, de-duplicated by name."""
    seen: dict[str, Param] = {}
    for action in ACTIONS:
        for param in action.params:
            seen.setdefault(param.name, param)
    return tuple(seen.values())


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def dispatch(service: "Service", action: str, params: dict[str, Any]) -> Any:
    """Run *action* against *service* with adapter-neutral *params*."""
    spec = resolve_action(action)
    _validate(spec, params)
    if spec.name == "start":
        return start(params, cwd=service.root)
    handler = _HANDLERS[spec.name]
    return handler(service, params)


def start(params: dict[str, Any], *, cwd: str | Path | None = None) -> dict[str, Any]:
    """Bootstrap without an existing :class:`Service` (the plane may not exist)."""
    from .service import Service

    path = params.get("path") or cwd or os.getcwd()
    return Service.init(
        path,
        name=params.get("name"),
        session=params.get("session"),
        base=params.get("base"),
        kind=params.get("kind") or "worker",
        main_branch=params.get("main_branch"),
        checks=parse_checks(params.get("checks") or []),
        force=bool(params.get("force")),
        no_unit=bool(params.get("no_unit")),
    )


def parse_checks(items: list[str]) -> list[dict[str, Any]]:
    checks = []
    for item in items:
        if "=" in item:
            name, command = item.split("=", 1)
            checks.append({"name": name.strip(), "command": command.strip(), "required": True})
        else:
            checks.append({"name": item, "command": item, "required": True})
    return checks


def doctor(root: Path) -> dict[str, Any]:
    from . import gitutil

    checks = [
        ("git", gitutil.is_git_repo(root)),
        ("config", (root / ".sliceme" / "config.json").is_file()),
        ("state_db", (root / ".sliceme" / "state.db").is_file()),
    ]
    return {
        "root": str(root),
        "checks": [{"name": name, "ok": ok} for name, ok in checks],
        "ok": all(ok for _, ok in checks),
    }


def _resolve_unit(service: "Service", params: dict[str, Any]) -> str:
    explicit = params.get("unit")
    if explicit:
        return str(explicit)
    return service.current_unit()["name"]


def _dispatch_status(service: "Service", p: dict[str, Any]) -> Any:
    if p.get("gc"):
        return service.gc()
    if p.get("health"):
        return doctor(service.root)
    if p.get("simulate"):
        return service.simulation(run_checks_flag=not p.get("no_checks"))
    if p.get("short"):
        return {"unit": service.current_unit()["name"]}
    if p.get("unit"):
        return service.unit_detail(p["unit"])
    return service.status()


def _dispatch_commit(service: "Service", p: dict[str, Any]) -> Any:
    unit = _resolve_unit(service, p)
    commit = service.commit(unit, p["message"])
    candidate = service.finish(unit, summary=p.get("summary"))
    return {"commit": commit, "candidate": candidate}


def _dispatch_integrate(service: "Service", p: dict[str, Any]) -> Any:
    return service.integrate(
        node=p.get("node"),
        acceptance=list(p.get("acceptance") or []),
        gpu=p.get("gpu") or "none",
        check_only=bool(p.get("check_only")),
        cleanup=p.get("cleanup") or "none",
        run_checks_flag=not p.get("no_checks"),
    )


def _dispatch_report(service: "Service", p: dict[str, Any]) -> Any:
    return service.report(narrative=p.get("narrative"), design=p.get("design"))


_HANDLERS = {
    "status": _dispatch_status,
    "commit": _dispatch_commit,
    "integrate": _dispatch_integrate,
    "report": _dispatch_report,
}


def _validate(spec: Action, params: dict[str, Any]) -> None:
    for param in spec.params:
        if param.required and not params.get(param.name):
            raise SlicemeError(f"{spec.name} requires --{param.name.replace('_', '-')}")
        value = params.get(param.name)
        if value and param.choices and value not in param.choices:
            raise SlicemeError(
                f"{spec.name} --{param.name.replace('_', '-')} must be one of: "
                + ", ".join(param.choices)
            )
