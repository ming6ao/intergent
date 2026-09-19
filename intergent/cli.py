"""Command line interface (``intergent`` / ``ig``).

The CLI is a thin adapter over :mod:`intergent.service`; it owns no state.  It
covers bootstrap, the human review loop, recovery, and diagnostics, as required
by ``docs/architecture.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .service import Service
from .util import IntergentError, find_repo_root, iso


def _print(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True, default=str))
    else:
        print(_human(data))


def _human(data: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(data, dict):
        lines = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.append(_human(value, indent + 1))
            else:
                lines.append(f"{pad}{key}: {value}")
        return "\n".join(lines)
    if isinstance(data, list):
        if not data:
            return f"{pad}(none)"
        lines = []
        for item in data:
            if isinstance(item, (dict, list)):
                lines.append(_human(item, indent + 1))
            else:
                lines.append(f"{pad}- {item}")
        return "\n".join(lines)
    return f"{pad}{data}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="intergent",
        description="Local coordination for parallel coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"intergent {__version__}")
    parser.add_argument("--root", help="workspace root (defaults to nearest .intergent)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    # init -----------------------------------------------------------------
    p = sub.add_parser("init", help="initialise an Intergent workspace in a git repo")
    p.add_argument("--main", dest="main_branch", help="main/integration branch (default: current)")
    p.add_argument("--base", help="base branch/ref for new worktrees (default: main)")
    p.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
        help="trusted verification command (repeatable)",
    )
    p.add_argument("--lease-ttl", type=int, default=1800, help="lease TTL in seconds")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")

    sub.add_parser("doctor", help="check git and workspace health")

    # agents ---------------------------------------------------------------
    p = sub.add_parser("agent", help="agent registry")
    ag = p.add_subparsers(dest="subcommand", required=True)
    p = ag.add_parser("register", help="register an agent")
    p.add_argument("name")
    p.add_argument("--model")
    p.add_argument("--parent")
    ag.add_parser("list", help="list agents")

    # sessions -------------------------------------------------------------
    p = sub.add_parser("session", help="sessions")
    ss = p.add_subparsers(dest="subcommand", required=True)
    p = ss.add_parser("create", help="create a session")
    p.add_argument("name")
    p.add_argument("--task")
    p.add_argument("--attachment", default="terminal")
    ss.add_parser("list", help="list sessions")

    # workspace ------------------------------------------------------------
    p = sub.add_parser("workspace", help="worktree-isolated units")
    ws = p.add_subparsers(dest="subcommand", required=True)
    p = ws.add_parser("create", help="create a unit with its own worktree and branch")
    p.add_argument("name")
    p.add_argument("--session")
    p.add_argument("--kind", default="worker", choices=["session", "worker"])
    p.add_argument("--base")
    p.add_argument("--agent")
    p.add_argument("--task")
    ws.add_parser("list", help="list units")
    p = ws.add_parser("current", help="show the unit for the current worktree")
    p.add_argument("--short", action="store_true", help="print only the unit name")
    p = ws.add_parser("show", help="show one unit")
    p.add_argument("unit")

    # intent ---------------------------------------------------------------
    p = sub.add_parser("declare", help="declare intent and acquire scope leases")
    p.add_argument("--unit", help="unit (defaults to the unit worktree containing cwd)")
    p.add_argument("--operation", required=True, help="add|extend|modify|replace|remove|rename|migrate")
    p.add_argument("--scope", action="append", default=[], metavar="KIND:KEY[=OP]")
    p.add_argument("--task")
    p.add_argument("--summary")

    p = sub.add_parser("check", help="dry-run conflict check (no leases taken)")
    p.add_argument("--unit", help="unit (defaults to the unit worktree containing cwd)")
    p.add_argument("--operation", required=True)
    p.add_argument("--scope", action="append", default=[])

    p = sub.add_parser("decide", help="resolve a needs_decision conflict")
    p.add_argument("intent_id", type=int)
    p.add_argument("--action", required=True, choices=["wait", "override", "redesign"])
    p.add_argument("--reason")

    p = sub.add_parser("override", help="audited override of the unit's current conflict")
    p.add_argument("--unit", help="unit (defaults to the unit worktree containing cwd)")
    p.add_argument("--reason", required=True)

    p = sub.add_parser("heartbeat", help="renew the unit's leases")
    p.add_argument("--unit", help="unit (defaults to the unit worktree containing cwd)")

    p = sub.add_parser("release", help="release the unit's leases and promote waiters")
    p.add_argument("--unit", help="unit (defaults to the unit worktree containing cwd)")

    p = sub.add_parser("rebase", help="rebase a unit worktree onto base/another branch")
    p.add_argument("--unit", help="unit (defaults to the unit worktree containing cwd)")
    p.add_argument("--onto")

    # candidate lifecycle --------------------------------------------------
    p = sub.add_parser("commit", help="commit all changes in a unit worktree")
    p.add_argument("--unit", help="unit (defaults to the unit worktree containing cwd)")
    p.add_argument("-m", "--message", required=True)

    p = sub.add_parser("finish", help="mark the unit's branch as a ready candidate")
    p.add_argument("--unit", help="unit (defaults to the unit worktree containing cwd)")
    p.add_argument("--summary")

    p = sub.add_parser("verify", help="run trusted checks pinned to a fingerprint")
    p.add_argument("candidate")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("simulate", help="plan waves and verify the combined tree")
    p.add_argument("--no-checks", action="store_true", help="plan only, do not run checks")

    p = sub.add_parser("review", help="show the review packet for a candidate")
    p.add_argument("candidate")

    p = sub.add_parser("approve", help="approve a verified candidate for landing")
    p.add_argument("candidate")
    p.add_argument("--reason")

    p = sub.add_parser("reject", help="reject a candidate")
    p.add_argument("candidate")
    p.add_argument("--reason")

    p = sub.add_parser("land", help="merge approved candidates into the local main branch")
    p.add_argument("candidate", nargs="*")
    p.add_argument("--all", action="store_true", dest="all_approved")
    p.add_argument("--no-checks", action="store_true")
    p.add_argument("--cleanup", action="store_true", help="remove landed unit worktrees")

    p = sub.add_parser("status", help="show units, candidates, leases, and wave plan")
    sub.add_parser("gc", help="prune worktrees for landed/released units")
    sub.add_parser("mcp", help="run the MCP stdio server for agents")
    return parser


def _root(args: argparse.Namespace, *, init: bool = False) -> Path:
    if args.root:
        return Path(args.root).resolve()
    if init:
        return Path.cwd().resolve()
    return find_repo_root()


def _parse_checks(items: list[str]) -> list[dict[str, Any]]:
    checks = []
    for item in items:
        if "=" in item:
            name, command = item.split("=", 1)
            checks.append({"name": name.strip(), "command": command.strip(), "required": True})
        else:
            checks.append({"name": item, "command": item, "required": True})
    return checks


def _normalize_argv(argv: list[str]) -> list[str]:
    """Allow the global ``--json`` / ``--root`` flags before or after the subcommand."""
    out: list[str] = []
    extras: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--json":
            extras.append(token)
        elif token == "--root":
            extras.append(token)
            if i + 1 < len(argv):
                extras.append(argv[i + 1])
                i += 1
        elif token.startswith("--root="):
            extras.append(token)
        else:
            out.append(token)
        i += 1
    return extras + out


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(_normalize_argv(raw))
    as_json = bool(getattr(args, "json", False))
    try:
        return _dispatch(args, as_json)
    except IntergentError as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130


def _dispatch(args: argparse.Namespace, as_json: bool) -> int:
    command = args.command
    if command == "init":
        root = _root(args, init=True)
        config = Service.init(
            root,
            main_branch=args.main_branch,
            base=args.base,
            checks=_parse_checks(args.check),
            lease_ttl_seconds=args.lease_ttl,
            force=args.force,
        )
        _print({"initialised": str(root), **config}, as_json)
        return 0
    if command == "doctor":
        return _doctor(_root(args), as_json)
    if command == "mcp":
        from .mcp import serve

        return serve(Service(_root(args)))

    service = Service(_root(args))
    try:
        return _dispatch_service(service, args, as_json)
    finally:
        service.close()


def _resolve_unit(service: Service, args: argparse.Namespace) -> str:
    explicit = getattr(args, "unit", None)
    if explicit:
        return explicit
    unit = service.current_unit()
    return unit["name"]


def _dispatch_service(service: Service, args: argparse.Namespace, as_json: bool) -> int:
    command = args.command
    if command == "agent":
        if args.subcommand == "register":
            _print(service.register_agent(args.name, model=args.model, parent=args.parent), as_json)
        else:
            _print(service.store.list_agents(), as_json)
    elif command == "session":
        if args.subcommand == "create":
            _print(service.create_session(args.name, task=args.task, attachment=args.attachment), as_json)
        else:
            _print(service.store.list_sessions(), as_json)
    elif command == "workspace":
        if args.subcommand == "create":
            _print(
                service.create_workspace(
                    args.name,
                    session=args.session,
                    kind=args.kind,
                    base=args.base,
                    agent=args.agent,
                    task=args.task,
                ),
                as_json,
            )
        elif args.subcommand == "show":
            _print(service.unit_detail(args.unit), as_json)
        elif args.subcommand == "current":
            unit = service.current_unit()
            if args.short:
                print(unit["name"])
            else:
                _print(unit, as_json)
        else:
            _print(service.list_units(), as_json)
    elif command == "declare":
        result = service.declare_intent(
            _resolve_unit(service, args),
            operation=args.operation,
            scope_specs=args.scope,
            task=args.task,
            summary=args.summary,
        )
        _print(result, as_json)
    elif command == "check":
        findings = service.check_conflicts(_resolve_unit(service, args), args.operation, args.scope)
        _print({"findings": [f.to_dict() for f in findings]}, as_json)
    elif command == "decide":
        _print(service.decide(args.intent_id, args.action, reason=args.reason), as_json)
    elif command == "override":
        unit = service.store.require_unit(_resolve_unit(service, args))
        request = service.store.get_lock_request_for_unit(int(unit["id"]))
        if request is None:
            raise IntergentError(f"unit {unit['name']} has no pending conflict")
        _print(service.decide(int(request["intent_id"]), "override", reason=args.reason), as_json)
    elif command == "heartbeat":
        _print(service.heartbeat(_resolve_unit(service, args)), as_json)
    elif command == "release":
        _print(service.release(_resolve_unit(service, args)), as_json)
    elif command == "rebase":
        _print(service.rebase(_resolve_unit(service, args), onto=args.onto), as_json)
    elif command == "commit":
        _print(service.commit(_resolve_unit(service, args), args.message), as_json)
    elif command == "finish":
        _print(service.finish(_resolve_unit(service, args), summary=args.summary), as_json)
    elif command == "verify":
        _print(service.verify(args.candidate, force=args.force), as_json)
    elif command == "simulate":
        _print(service.simulation(run_checks_flag=not args.no_checks), as_json)
    elif command == "review":
        _print(service.review(args.candidate), as_json)
    elif command == "approve":
        _print(service.approve(args.candidate, reason=args.reason), as_json)
    elif command == "reject":
        _print(service.reject(args.candidate, reason=args.reason), as_json)
    elif command == "land":
        results = service.land(
            args.candidate,
            all_approved=args.all_approved,
            run_checks_flag=not args.no_checks,
            cleanup=args.cleanup,
        )
        _print({"landed": results}, as_json)
    elif command == "status":
        _print(service.status(), as_json)
    elif command == "gc":
        _print(service.gc(), as_json)
    else:  # pragma: no cover
        raise IntergentError(f"unhandled command: {command}")
    return 0


def _doctor(root: Path, as_json: bool) -> int:
    from . import gitutil

    report: dict[str, Any] = {"root": str(root)}
    checks = []
    checks.append(("git", gitutil.is_git_repo(root)))
    cfg = root / ".intergent" / "config.json"
    checks.append(("config", cfg.is_file()))
    checks.append(("state_db", (root / ".intergent" / "state.db").is_file()))
    report["checks"] = [{"name": name, "ok": ok} for name, ok in checks]
    report["ok"] = all(ok for _, ok in checks)
    if as_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for name, ok in checks:
            print(f"[{'ok' if ok else 'fail'}] {name}")
        print("doctor:", "healthy" if report["ok"] else "problems found")
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
