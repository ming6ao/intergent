"""MCP stdio server (agent hot loop).

Thin JSON-RPC adapter over :class:`intergent.service.Service`.  MCP uses
newline-delimited JSON over stdio; the tool set is intentionally small to limit
context bloat (``docs/architecture.md``).
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from .service import Service
from .util import IntergentError

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "intergent", "version": "0.1.0"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "register_agent",
        "description": "Register the calling agent with the local coordinator.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "model": {"type": "string"},
                "parent": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "create_workspace",
        "description": "Create a unit with its own git worktree and branch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "session": {"type": "string"},
                "kind": {"type": "string", "enum": ["session", "worker"]},
                "base": {"type": "string"},
                "agent": {"type": "string"},
                "task": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "register_child",
        "description": "Create a child worker worktree under a parent unit's session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "parent_unit": {"type": "string"},
                "name": {"type": "string"},
                "agent": {"type": "string"},
                "task": {"type": "string"},
            },
            "required": ["parent_unit", "name"],
        },
    },
    {
        "name": "declare_intent",
        "description": "Declare scopes and operations; acquires scope leases or queues.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "unit": {"type": "string"},
                "operation": {
                    "type": "string",
                    "enum": ["add", "extend", "modify", "replace", "remove", "rename", "migrate"],
                },
                "scopes": {"type": "array", "items": {"type": "string"}},
                "task": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["unit", "operation", "scopes"],
        },
    },
    {
        "name": "check_conflicts",
        "description": "Dry-run conflict detection without taking leases.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "unit": {"type": "string"},
                "operation": {"type": "string"},
                "scopes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["unit", "operation", "scopes"],
        },
    },
    {
        "name": "claim_scope",
        "description": "Alias for declare_intent: leases are acquired when intent is declared.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "unit": {"type": "string"},
                "operation": {"type": "string"},
                "scopes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["unit", "operation", "scopes"],
        },
    },
    {
        "name": "heartbeat",
        "description": "Renew the unit's leases.",
        "inputSchema": {
            "type": "object",
            "properties": {"unit": {"type": "string"}},
            "required": ["unit"],
        },
    },
    {
        "name": "release",
        "description": "Release the unit's leases and promote queued waiters.",
        "inputSchema": {
            "type": "object",
            "properties": {"unit": {"type": "string"}},
            "required": ["unit"],
        },
    },
    {
        "name": "commit_workspace",
        "description": "Commit all changes in the unit worktree.",
        "inputSchema": {
            "type": "object",
            "properties": {"unit": {"type": "string"}, "message": {"type": "string"}},
            "required": ["unit", "message"],
        },
    },
    {
        "name": "finish_workspace",
        "description": "Mark the unit's branch as a ready candidate.",
        "inputSchema": {
            "type": "object",
            "properties": {"unit": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["unit"],
        },
    },
    {
        "name": "verify",
        "description": "Run trusted checks at the candidate commit; result is fingerprint-pinned.",
        "inputSchema": {
            "type": "object",
            "properties": {"candidate": {"type": "string"}, "force": {"type": "boolean"}},
            "required": ["candidate"],
        },
    },
    {
        "name": "status",
        "description": "Show units, candidates, lease queue, and wave plan.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "current_workspace",
        "description": "Return the unit whose worktree contains the server's cwd.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "submit",
        "description": "Build the review packet for a candidate. Landing requires human approval.",
        "inputSchema": {
            "type": "object",
            "properties": {"candidate": {"type": "string"}},
            "required": ["candidate"],
        },
    },
]

# `unit` is optional on the hot-loop tools: when the server runs inside a unit
# worktree, the service resolves the current unit from the cwd.
for _tool in TOOLS:
    _required = _tool.get("inputSchema", {}).get("required")
    if _required and "unit" in _required:
        _required.remove("unit")


def serve(service: Service) -> int:
    """Serve MCP over stdin/stdout until EOF or exit."""
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = _handle(service, message)
        if response is not None:
            out.write(json.dumps(response, default=str) + "\n")
            out.flush()
        if message.get("method") == "exit":
            break
    return 0


def _handle(service: Service, message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    if method and method.startswith("notifications/"):
        return None
    if method == "initialize":
        return _result(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            payload = _call(service, name, arguments)
            text = json.dumps(payload, indent=2, default=str)
            return _result(
                msg_id,
                {"content": [{"type": "text", "text": text}], "isError": False},
            )
        except (IntergentError, KeyError, TypeError) as exc:
            return _result(
                msg_id,
                {
                    "content": [{"type": "text", "text": f"error: {exc}"}],
                    "isError": True,
                },
            )
    if method == "shutdown":
        return _result(msg_id, {})
    if method == "exit":
        return None
    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return None


def _result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _call(service: Service, name: str, args: dict[str, Any]) -> Any:
    handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
        "register_agent": lambda a: service.register_agent(
            a["name"], model=a.get("model"), parent=a.get("parent")
        ),
        "create_workspace": lambda a: service.create_workspace(
            a["name"],
            session=a.get("session"),
            kind=a.get("kind", "worker"),
            base=a.get("base"),
            agent=a.get("agent"),
            task=a.get("task"),
        ),
        "register_child": lambda a: _register_child(service, a),
        "declare_intent": lambda a: service.declare_intent(
            _unit(service, a),
            operation=a["operation"],
            scope_specs=list(a["scopes"]),
            task=a.get("task"),
            summary=a.get("summary"),
        ),
        "check_conflicts": lambda a: {
            "findings": [
                f.to_dict()
                for f in service.check_conflicts(
                    _unit(service, a), a["operation"], list(a["scopes"])
                )
            ]
        },
        "claim_scope": lambda a: service.declare_intent(
            _unit(service, a), operation=a["operation"], scope_specs=list(a["scopes"])
        ),
        "heartbeat": lambda a: service.heartbeat(_unit(service, a)),
        "release": lambda a: service.release(_unit(service, a)),
        "commit_workspace": lambda a: service.commit(_unit(service, a), a["message"]),
        "finish_workspace": lambda a: service.finish(_unit(service, a), summary=a.get("summary")),
        "verify": lambda a: service.verify(a["candidate"], force=bool(a.get("force"))),
        "status": lambda a: service.status(),
        "current_workspace": lambda a: service.current_unit(),
        "submit": lambda a: service.review(a["candidate"]),
    }
    handler = handlers.get(name)
    if handler is None:
        raise IntergentError(f"unknown tool: {name}")
    return handler(args)


def _unit(service: Service, args: dict[str, Any]) -> str:
    """Use an explicit unit, else resolve the worktree containing the server cwd."""
    explicit = args.get("unit")
    if explicit:
        return str(explicit)
    return service.current_unit()["name"]


def _register_child(service: Service, args: dict[str, Any]) -> dict[str, Any]:
    parent = service.store.require_unit(args["parent_unit"])
    session = service.store.get_session(int(parent["session_id"])) if parent.get("session_id") else None
    return service.create_workspace(
        args["name"],
        session=(session or {}).get("name"),
        kind="worker",
        agent=args.get("agent"),
        task=args.get("task"),
    )
