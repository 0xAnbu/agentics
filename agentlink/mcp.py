"""MCP server over Streamable HTTP (stateless, JSON responses) at ``/mcp``.

Lets Claude Code and other MCP clients use AgentLink as native tools:

    claude mcp add --transport http agentlink https://HOST/mcp \
        --header "Authorization: Bearer al_..."

Only the request/response subset of the spec is needed (initialize, ping,
tools/list, tools/call, notifications). No server-initiated SSE stream: GET -> 405.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__, coordination, directory, messages
from . import validation as v
from .errors import AppError, BadRequest, Forbidden
from .hub import Hub, get_hub
from .security import Principal

log = logging.getLogger("agentlink.mcp")

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "AgentLink is the coordination bus for agents working on the same GitHub project. "
    "Start with whoami, then inbox(wait=20) to pick up mentions and assigned tasks. "
    "Before editing files, acquire_lease on the path; release it when you push. "
    "Claim tasks before working on them. Record decisions with set_context or kind=decision. "
    "Keep messages short; put structured details in data/refs. Always pass after=next_after when "
    "reading to receive only new messages."
)

S = {"type": "string"}
INT = {"type": "integer"}
CH = {"type": "string", "description": "channel name"}
REFS = {
    "type": "array",
    "items": {"type": "object"},
    "description": 'GitHub refs, e.g. [{"repo":"o/r","pr":12},{"path":"src/a.py","line":40}]',
}


def _tool(name: str, desc: str, props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": desc,
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("whoami", "Your agent identity, role and channels.", {}),
    _tool("list_channels", "Channels you can read, with last message id.", {}),
    _tool(
        "create_channel",
        "Create a channel (optionally linked to a GitHub repo for webhook events).",
        {
            "name": S,
            "repo": {"type": "string", "description": "owner/name"},
            "description": S,
            "members": {"type": "array", "items": S},
        },
        ["name"],
    ),
    _tool(
        "read_messages",
        "Read a channel incrementally. Pass after=next_after from the previous call; "
        "wait=N long-polls up to N s for new messages. Omit after to get the latest `limit`.",
        {
            "channel": CH,
            "after": INT,
            "limit": INT,
            "wait": INT,
            "thread": INT,
            "kinds": {"type": "string", "description": "comma list, e.g. task,decision"},
            "format": {"type": "string", "enum": ["text", "json"]},
        },
        ["channel"],
    ),
    _tool(
        "post_message",
        "Post to a channel. kind: chat|task|status|question|answer|decision|handoff|"
        "review|alert|context. @name in body mentions an agent (lands in their inbox).",
        {
            "channel": CH,
            "body": S,
            "kind": S,
            "data": {"type": "object"},
            "refs": REFS,
            "tags": {"type": "array", "items": S},
            "reply_to": INT,
            "mentions": {"type": "array", "items": S},
            "client_id": {"type": "string", "description": "idempotency key for safe retries"},
        },
        ["channel", "body"],
    ),
    _tool(
        "inbox",
        "Messages mentioning you since `after`, plus your open tasks. Use wait to long-poll.",
        {"after": INT, "wait": INT, "limit": INT, "format": {"type": "string", "enum": ["text", "json"]}},
    ),
    _tool(
        "feed",
        "All new messages across your channels since `after`.",
        {"after": INT, "wait": INT, "limit": INT, "format": {"type": "string", "enum": ["text", "json"]}},
    ),
    _tool(
        "create_task",
        "Create a task (priority 1=high..3=low). Assigning mentions the assignee.",
        {"channel": CH, "title": S, "description": S, "priority": INT, "assignee": S, "refs": REFS},
        ["channel", "title"],
    ),
    _tool(
        "list_tasks",
        "Open tasks in a channel (status filter: comma list).",
        {"channel": CH, "status": S, "assignee": S},
        ["channel"],
    ),
    _tool(
        "claim_task",
        "Atomically claim an open task. Fails with the current owner if taken.",
        {"task_id": INT},
        ["task_id"],
    ),
    _tool(
        "update_task",
        "Change status (open|claimed|in_progress|blocked|review|done|cancelled), "
        "assignee, priority, title or description. note is added to the channel log.",
        {
            "task_id": INT,
            "status": S,
            "assignee": S,
            "priority": INT,
            "title": S,
            "description": S,
            "note": S,
            "if_version": INT,
        },
        ["task_id"],
    ),
    _tool(
        "acquire_lease",
        "Lock a path/area (e.g. src/auth/) so others avoid editing it. Renew by "
        "calling again. Conflicts report the holder.",
        {"channel": CH, "resource": S, "ttl_seconds": INT, "note": S},
        ["channel", "resource"],
    ),
    _tool("release_lease", "Release a lease you hold.", {"channel": CH, "resource": S}, ["channel", "resource"]),
    _tool("list_leases", "Active leases in a channel.", {"channel": CH}, ["channel"]),
    _tool(
        "get_context",
        "Shared project memory: get one key, or list keys by prefix.",
        {"channel": CH, "key": S, "prefix": S},
        ["channel"],
    ),
    _tool(
        "set_context",
        "Set a shared key (JSON value). if_version gives optimistic concurrency.",
        {"channel": CH, "key": S, "value": {}, "if_version": INT},
        ["channel", "key", "value"],
    ),
    _tool(
        "heartbeat",
        "Report presence: status online|busy|idle|offline and current activity.",
        {"status": S, "activity": S, "channel": CH},
    ),
    _tool("presence", "Who is online and what they are doing.", {}),
]

WRITE_TOOLS = {
    "create_channel",
    "post_message",
    "create_task",
    "claim_task",
    "update_task",
    "acquire_lease",
    "release_lease",
    "set_context",
    "heartbeat",
}
POLL_TOOLS = {"read_messages", "inbox", "feed"}


def _pick(args: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: args[k] for k in keys if k in args and args[k] is not None}


def _dispatch(h: Hub, p: Principal, name: str, a: dict[str, Any]) -> Any:
    db, n = h.db, h.notifier
    table: dict[str, Callable[[], Any]] = {
        "whoami": lambda: directory.whoami(db, p),
        "list_channels": lambda: directory.list_channels(db, p),
        "create_channel": lambda: directory.create_channel(db, p, a),
        "post_message": lambda: messages.post_message(
            db,
            p,
            a.get("channel", ""),
            _pick(a, "body", "kind", "data", "refs", "tags", "reply_to", "mentions", "client_id"),
            notifier=n,
        ),
        "create_task": lambda: coordination.create_task(db, p, a.get("channel", ""), a, n),
        "list_tasks": lambda: coordination.list_tasks(db, p, a.get("channel", ""), a),
        "claim_task": lambda: coordination.claim_task(db, p, v.integer(a.get("task_id"), "task_id", lo=1), n),
        "update_task": lambda: coordination.update_task(
            db,
            p,
            v.integer(a.get("task_id"), "task_id", lo=1),
            _pick(a, "status", "assignee", "priority", "title", "description", "note", "if_version"),
            n,
        ),
        "acquire_lease": lambda: coordination.acquire_lease(db, p, a.get("channel", ""), a, n),
        "release_lease": lambda: coordination.release_lease(db, p, a.get("channel", ""), a.get("resource"), n),
        "list_leases": lambda: coordination.list_leases(db, p, a.get("channel", "")),
        "get_context": lambda: (
            coordination.kv_get(db, p, a.get("channel", ""), a["key"])
            if a.get("key")
            else coordination.kv_list(db, p, a.get("channel", ""), a.get("prefix") or "")
        ),
        "set_context": lambda: coordination.kv_set(db, p, a.get("channel", ""), a.get("key"), a, n),
        "heartbeat": lambda: coordination.heartbeat(db, p, a),
        "presence": lambda: coordination.presence(db, p),
    }
    if name not in table:
        raise BadRequest(f"unknown tool '{name}'")
    return table[name]()


def _poll_fetch(h: Hub, p: Principal, name: str, a: dict[str, Any]) -> Callable[[], dict[str, Any]]:
    query = {k: a[k] for k in ("after", "limit", "thread", "kinds") if a.get(k) is not None}
    if name == "read_messages":
        return lambda: messages.channel_messages(h.db, p, a.get("channel", ""), query)
    if name == "inbox":
        return lambda: messages.inbox(h.db, p, query)
    return lambda: messages.feed(h.db, p, query)


async def call_tool(h: Hub, p: Principal, name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if name in WRITE_TOOLS:
            await run_in_threadpool(h.limiter.check_write, p)
        else:
            h.limiter.check_read(p)
        if name in POLL_TOOLS:
            wait = v.wait_seconds(args.get("wait"), h.settings.max_wait_seconds)
            result = await messages.long_poll(_poll_fetch(h, p, name, args), wait, h.notifier, h.settings.poll_interval)
            text = (
                json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                if args.get("format") == "json"
                else messages.as_text(result)
            )
        else:
            result = await run_in_threadpool(_dispatch, h, p, name, args)
            text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except AppError as exc:
        return {"content": [{"type": "text", "text": json.dumps(exc.to_dict(), ensure_ascii=False)}], "isError": True}


def _rpc_error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


async def handle_rpc(h: Hub, p: Principal, msg: Any) -> dict[str, Any] | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _rpc_error(None, -32600, "invalid request")
    method, id_ = msg["method"], msg.get("id")
    params = msg.get("params") or {}
    if "id" not in msg:  # notification: no response body
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agentlink", "version": __version__},
                "instructions": INSTRUCTIONS,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return _rpc_error(id_, -32602, "invalid params")
        if name not in {t["name"] for t in TOOLS}:
            return _rpc_error(id_, -32602, f"unknown tool: {name}")
        return {"jsonrpc": "2.0", "id": id_, "result": await call_tool(h, p, name, args)}
    return _rpc_error(id_, -32601, f"method not found: {method}")


def _check_origin(request: Request, h: Hub) -> None:
    """DNS-rebinding defence required by the MCP spec: browsers send Origin; CLI agents do not."""
    origin = request.headers.get("origin")
    if not origin:
        return
    origin = origin.rstrip("/")
    same = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if origin != same and origin not in h.settings.allowed_origins:
        raise Forbidden("origin not allowed")


async def mcp_endpoint(request: Request) -> Response:
    if request.method != "POST":
        return Response(status_code=405, headers={"Allow": "POST"})
    from .api import authenticate, read_json  # local import avoids a cycle

    hub = await run_in_threadpool(get_hub)
    _check_origin(request, hub)
    _, p = await authenticate(request, write=False)
    payload = await read_json(request, hub)
    if isinstance(payload, list):
        responses = [r for r in [await handle_rpc(hub, p, m) for m in payload] if r is not None]
        return JSONResponse(responses) if responses else Response(status_code=202)
    resp = await handle_rpc(hub, p, payload)
    return JSONResponse(resp) if resp is not None else Response(status_code=202)
