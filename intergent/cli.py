"""Command line interface (``intergent`` / ``ig``).

The CLI is a thin, *generated* adapter over :mod:`intergent.surface`.  It owns
no verbs of its own apart from ``mcp``: every action is built from
:data:`intergent.surface.ACTIONS`, so the CLI can never drift from the agent
tool surface.  See ``docs/architecture.md`` ("one engine, many adapters").
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__, surface
from .service import Service
from .util import IntergentError, find_repo_root


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


def _flag(param: surface.Param) -> str:
    return "--" + (param.flag or param.name.replace("_", "-"))


def _add_action(sub: Any, action: surface.Action) -> None:
    parser = sub.add_parser(
        action.name,
        help=action.summary,
        description=action.summary,
        aliases=list(action.aliases),
    )
    for param in action.params:
        if param.positional:
            parser.add_argument(
                param.name,
                nargs="?" if not param.required else None,
                help=param.help,
                choices=param.choices or None,
            )
            continue
        kwargs: dict[str, Any] = {"dest": param.name, "help": param.help, "default": None}
        if param.type == "boolean":
            kwargs["action"] = "store_true"
            kwargs["default"] = False
        elif param.type == "int":
            kwargs["type"] = int
        elif param.type == "list":
            kwargs["action"] = "append"
            kwargs["default"] = []
        if param.choices:
            kwargs["choices"] = param.choices
        if param.required:
            kwargs["required"] = True
        names = [_flag(param)]
        if param.name == "message":
            names.append("-m")
        parser.add_argument(*names, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="intergent",
        description="Local coordination for parallel coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"intergent {__version__}")
    parser.add_argument("--root", help="workspace root (defaults to nearest .intergent)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    sub = parser.add_subparsers(dest="action", required=True, metavar="ACTION")
    for action in surface.ACTIONS:
        _add_action(sub, action)

    sub.add_parser("mcp", help="run the MCP stdio server for agents")
    return parser


def _normalize_argv(argv: list[str]) -> list[str]:
    """Allow the global ``--json`` / ``--root`` flags before or after the action."""
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


def _root(args: argparse.Namespace) -> Path:
    if args.root:
        return Path(args.root).resolve()
    return find_repo_root()


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
    action = args.action
    if action == "mcp":
        from .mcp import serve

        return serve(Service(_root(args)))

    # `start` bootstraps the plane, so no Service exists yet.
    if surface.resolve_action(action).name == "start":
        result = surface.start(vars(args), cwd=Path.cwd())
        _print(result, as_json)
        return 0

    service = Service(_root(args))
    try:
        result = surface.dispatch(
            service, action, {**vars(args), "_human": True}
        )
    finally:
        service.close()

    if action == "status" and getattr(args, "short", False) and not as_json:
        print(result["unit"] if isinstance(result, dict) else result)
    else:
        _print(result, as_json)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
