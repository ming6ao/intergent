"""MCP stdio server (agent hot loop).

Thin JSON-RPC adapter over :mod:`intergent.service`, generated from
:mod:`intergent.surface`.  The server exposes exactly **one** tool, ``ig``,
parameterized by an ``action`` enum, instead of one tool per verb.  This keeps
the agent's context small and makes it impossible for the MCP surface to drift
from the CLI (both are renders of the same action registry).

Human-only actions (``submit`` and the ``review`` approval flags) are never in
the tool schema and are rejected if called by name.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__, surface
from .service import Service
from .surface import Param
from .util import IntergentError

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "intergent", "version": __version__}
TOOL_NAME = "ig"


def _param_schema(param: Param) -> dict[str, Any]:
    if param.type == "boolean":
        schema: dict[str, Any] = {"type": "boolean"}
    elif param.type == "int":
        schema = {"type": "integer"}
    elif param.type == "list":
        schema = {"type": "array", "items": {"type": "string"}}
    else:
        schema = {"type": "string"}
    if param.choices:
        schema["enum"] = list(param.choices)
    schema["description"] = param.help
    return schema


def build_tool() -> dict[str, Any]:
    actions = surface.agent_actions()
    actions_line = "; ".join(f"{a.name}: {a.summary}" for a in actions)
    properties: dict[str, Any] = {
        "action": {
            "type": "string",
            "enum": [a.name for a in actions],
            "description": "Lifecycle action. " + actions_line,
        }
    }
    for param in surface.agent_params():
        properties[param.name] = _param_schema(param)
    return {
        "name": TOOL_NAME,
        "description": (
            "Intergent unit lifecycle. One tool, many actions: "
            + actions_line
            + ". Call it before editing (declare), then commit, verify, and "
            "review. Landing is a human action, never exposed here."
        ),
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": ["action"],
        },
    }


TOOLS: list[dict[str, Any]] = [build_tool()]


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
            payload = call(service, name, arguments)
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


def call(service: Service, name: str | None, args: dict[str, Any]) -> Any:
    """Dispatch one tool call.

    Accepts the generic ``ig`` tool (``{"action": ...}``) and, for ergonomics,
    a direct action name as the tool name.
    """
    if name == TOOL_NAME:
        action = args.get("action")
        if not action:
            raise IntergentError(f"{TOOL_NAME} requires an 'action'")
        params = {k: v for k, v in args.items() if k != "action"}
    elif name:
        action = name
        params = dict(args)
    else:
        raise IntergentError("missing tool name")
    return surface.dispatch(service, action, {**params, "_human": False})
