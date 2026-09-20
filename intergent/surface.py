"""The one action surface, shared by every adapter.

The CLI, the MCP server, and the pi extension all derive their verbs/tools from
:data:`ACTIONS`, so a surface can never exist in one adapter and not another.
:func:`dispatch` is the single implementation every adapter calls; adapters only
parse arguments and render results.

Names are deliberately few and overloaded by flags: ``declare --renew`` is a
heartbeat, ``declare --dry-run`` is a conflict check, ``commit --sync`` rebases
first, ``status --simulate`` plans waves.  See ``docs/agents.md`` for the map
from the old surface.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .util import IntergentError

if TYPE_CHECKING:  # pragma: no cover
    from .service import Service


@dataclass(frozen=True)
class Param:
    """One argument, rendered as a CLI flag/positional and a tool property."""

    name: str
    type: str  # string | boolean | int | list
    help: str
    required: bool = False
    choices: tuple[str, ...] = ()
    positional: bool = False
    human_only: bool = False
    flag: str | None = None  # CLI flag/stem; defaults to name with dashes


@dataclass(frozen=True)
class Action:
    name: str
    summary: str
    params: tuple[Param, ...] = ()
    visibility: str = "both"  # both | human | agent
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
            Param("agent", "string", "agent name to attribute the unit to"),
            Param("kind", "string", "unit kind", choices=("session", "worker")),
            Param("base", "string", "base branch/ref for new worktrees"),
            Param("task", "string", "task description stored on the session"),
            Param("main_branch", "string", "main/integration branch (default: current)", flag="main"),
            Param("checks", "list", "trusted check NAME=COMMAND (repeatable)", flag="check"),
            Param("lease_ttl", "int", "lease TTL in seconds"),
            Param("force", "boolean", "overwrite an existing config"),
        ),
    ),
    Action(
        name="status",
        summary="show units, candidates, leases, waves, and health",
        params=(
            Param("unit", "string", "show one unit instead of the summary"),
            Param("short", "boolean", "print only the current unit name"),
            Param("simulate", "boolean", "plan waves and verify the combined tree"),
            Param("health", "boolean", "check git/plane health"),
            Param("gc", "boolean", "prune worktrees for landed/released units"),
            Param("no_checks", "boolean", "with --simulate: plan only, do not run checks"),
        ),
    ),
    Action(
        name="declare",
        summary="declare scopes and acquire leases (also checks, renews, releases, decides)",
        params=(
            Param("unit", "string", "unit (defaults to the worktree containing cwd)"),
            Param("operation", "string", "scope operation", choices=(
                "add", "extend", "modify", "replace", "remove", "rename", "migrate"
            )),
            Param("scopes", "list", "scope spec KIND:KEY[=OP] (repeatable)", flag="scope"),
            Param("task", "string", "task label for the intent"),
            Param("summary", "string", "summary for the intent"),
            Param("dry_run", "boolean", "conflict check only; take no lease"),
            Param("renew", "boolean", "renew this unit's leases"),
            Param("release", "boolean", "release this unit's leases and promote waiters"),
            Param("decide", "string", "resolve a needs_decision conflict", choices=(
                "wait", "override", "redesign"
            )),
            Param("intent_id", "int", "intent to resolve (default: the unit's pending conflict)"),
            Param("reason", "string", "reason recorded with --decide"),
        ),
    ),
    Action(
        name="commit",
        summary="commit the worktree and register the candidate",
        params=(
            Param("unit", "string", "unit (defaults to the worktree containing cwd)"),
            Param("message", "string", "commit message", required=True),
            Param("summary", "string", "candidate summary for review"),
            Param("sync", "boolean", "rebase onto base before committing"),
            Param("onto", "string", "with --sync: target branch/ref"),
        ),
    ),
    Action(
        name="verify",
        summary="run trusted checks at a candidate's exact commit (fingerprint-pinned)",
        params=(
            Param("candidate", "string", "candidate id or unit name", required=True, positional=True),
            Param("force", "boolean", "re-run even if a cached result exists"),
        ),
    ),
    Action(
        name="review",
        summary="show a candidate's review packet; humans may approve, reject, or land it",
        params=(
            Param("candidate", "string", "candidate id or unit name", positional=True),
            Param("approve", "boolean", "approve the candidate for landing", human_only=True),
            Param("reject", "boolean", "reject the candidate", human_only=True),
            Param("land", "boolean", "stage approved candidates on local main", human_only=True),
            Param("all", "boolean", "with --land: land every approved candidate", human_only=True),
            Param("draft", "boolean", "with --land: stage the draft without committing (waits for approval)", human_only=True),
            Param("commit", "boolean", "with --land: commit the pending landing draft", human_only=True),
            Param("abort", "boolean", "with --land: discard the pending landing draft", human_only=True),
            Param("reason", "string", "reason recorded with --approve/--reject", human_only=True),
            Param("no_checks", "boolean", "with --land: skip the pre-land combined-tree checks", human_only=True),
            Param("cleanup", "boolean", "with --land: remove landed unit worktrees", human_only=True),
        ),
    ),
    Action(
        name="submit",
        summary="approve a candidate and land it (stages a draft by default; human action)",
        visibility="human",
        params=(
            Param("candidate", "string", "candidate id or unit name", required=True, positional=True),
            Param("reason", "string", "reason recorded with the approval"),
            Param("draft", "boolean", "stage the draft on main instead of committing (waits for approval)"),
            Param("no_checks", "boolean", "skip the pre-land combined-tree checks"),
            Param("cleanup", "boolean", "remove the landed unit worktree"),
        ),
    ),
)

ACTION_BY_NAME: dict[str, Action] = {a.name: a for a in ACTIONS}
_ALIAS_TO_NAME: dict[str, str] = {alias: a.name for a in ACTIONS for alias in a.aliases}


def resolve_action(name: str) -> Action:
    canonical = _ALIAS_TO_NAME.get(name, name)
    action = ACTION_BY_NAME.get(canonical)
    if action is None:
        raise IntergentError(f"unknown action: {name}")
    return action


def agent_actions() -> tuple[Action, ...]:
    return tuple(a for a in ACTIONS if a.visibility in {"both", "agent"})


def agent_params() -> tuple[Param, ...]:
    """Union of non-human parameters across agent-visible actions."""
    seen: dict[str, Param] = {}
    for action in agent_actions():
        for param in action.params:
            if param.human_only:
                continue
            seen.setdefault(param.name, param)
    return tuple(seen.values())


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def dispatch(service: "Service", action: str, params: dict[str, Any]) -> Any:
    """Run *action* against *service* with adapter-neutral *params*."""
    spec = resolve_action(action)
    if spec.visibility == "human" and not params.get("_human"):
        raise IntergentError(f"action '{spec.name}' is a human action; it is not agent-callable")
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
        agent=params.get("agent"),
        session=params.get("session"),
        base=params.get("base"),
        kind=params.get("kind") or "worker",
        main_branch=params.get("main_branch"),
        checks=parse_checks(params.get("checks") or []),
        lease_ttl_seconds=params.get("lease_ttl") or 1800,
        force=bool(params.get("force")),
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
        ("config", (root / ".intergent" / "config.json").is_file()),
        ("state_db", (root / ".intergent" / "state.db").is_file()),
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


def _dispatch_declare(service: "Service", p: dict[str, Any]) -> Any:
    unit = _resolve_unit(service, p)
    if p.get("renew"):
        return service.heartbeat(unit)
    if p.get("release"):
        return service.release(unit)
    if p.get("decide"):
        intent_id = p.get("intent_id")
        if intent_id is None:
            row = service.store.require_unit(unit)
            request = service.store.get_lock_request_for_unit(int(row["id"]))
            if request is None:
                raise IntergentError(f"unit {row['name']} has no pending conflict")
            intent_id = int(request["intent_id"])
        return service.decide(int(intent_id), p["decide"], reason=p.get("reason"))
    operation = p.get("operation")
    if not operation:
        raise IntergentError("declare requires --operation (or --dry-run/--renew/--release/--decide)")
    scopes = list(p.get("scopes") or [])
    if p.get("dry_run"):
        findings = service.check_conflicts(unit, operation, scopes)
        return {"findings": [f.to_dict() for f in findings]}
    return service.declare_intent(
        unit,
        operation=operation,
        scope_specs=scopes,
        task=p.get("task"),
        summary=p.get("summary"),
    )


def _dispatch_commit(service: "Service", p: dict[str, Any]) -> Any:
    unit = _resolve_unit(service, p)
    if p.get("sync"):
        service.rebase(unit, onto=p.get("onto"))
    commit = service.commit(unit, p["message"])
    candidate = service.finish(unit, summary=p.get("summary"))
    return {"commit": commit, "candidate": candidate}


def _dispatch_review(service: "Service", p: dict[str, Any]) -> Any:
    candidate = p.get("candidate")
    if p.get("approve"):
        if not candidate:
            raise IntergentError("review --approve requires a candidate")
        return service.approve(candidate, reason=p.get("reason"))
    if p.get("reject"):
        if not candidate:
            raise IntergentError("review --reject requires a candidate")
        return service.reject(candidate, reason=p.get("reason"))
    if p.get("land"):
        refs = [candidate] if candidate else []
        return {
            "landed": service.land(
                refs,
                all_approved=bool(p.get("all")) or not refs,
                run_checks_flag=not p.get("no_checks"),
                cleanup=bool(p.get("cleanup")),
                draft=True if p.get("draft") else False if p.get("commit") or p.get("abort") else None,
                commit_draft=bool(p.get("commit")),
                abort_draft=bool(p.get("abort")),
            )
        }
    if not candidate:
        raise IntergentError("review requires a candidate (or --land --all)")
    return service.review(candidate)


def _dispatch_submit(service: "Service", p: dict[str, Any]) -> Any:
    service.approve(p["candidate"], reason=p.get("reason"))
    return {
        "landed": service.land(
            [p["candidate"]],
            run_checks_flag=not p.get("no_checks"),
            cleanup=bool(p.get("cleanup")),
            draft=True if p.get("draft") else None,
        )
    }


_HANDLERS = {
    "status": _dispatch_status,
    "declare": _dispatch_declare,
    "commit": _dispatch_commit,
    "verify": lambda s, p: s.verify(p["candidate"], force=bool(p.get("force"))),
    "review": _dispatch_review,
    "submit": _dispatch_submit,
}


def _validate(spec: Action, params: dict[str, Any]) -> None:
    for param in spec.params:
        if param.required and not params.get(param.name):
            raise IntergentError(f"{spec.name} requires --{param.name.replace('_', '-')}")
        value = params.get(param.name)
        if value and param.choices and value not in param.choices:
            raise IntergentError(
                f"{spec.name} --{param.name.replace('_', '-')} must be one of: "
                + ", ".join(param.choices)
            )
