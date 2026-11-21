"""REST transport. Each handler: authenticate -> rate-limit -> call a service -> JSON.

No business rules live here; they are in directory/messages/coordination/archive.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from . import __version__, archive, coordination, directory, github, messages
from . import validation as v
from .errors import AppError, BadRequest, TooLarge
from .hub import Hub, get_hub
from .security import Principal

log = logging.getLogger("agentlink.api")
STATIC = Path(__file__).parent / "static"

Handler = Callable[[Hub, Principal, Request, Any], Any]


async def read_json(request: Request, hub: Hub) -> Any:
    raw = await request.body()
    if len(raw) > hub.settings.max_body_bytes:
        raise TooLarge(f"body exceeds {hub.settings.max_body_bytes} bytes")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise BadRequest("invalid JSON body") from None


async def authenticate(request: Request, *, write: bool) -> tuple[Hub, Principal]:
    hub = await run_in_threadpool(get_hub)
    p = await run_in_threadpool(hub.auth.authenticate, request.headers.get("authorization"))
    if write:
        await run_in_threadpool(hub.limiter.check_write, p)
    else:
        hub.limiter.check_read(p)
    return hub, p


def endpoint(fn: Handler, *, write: bool = False) -> Callable[[Request], Awaitable[Response]]:
    """Wrap a synchronous service call; the DB work runs in the thread pool."""

    async def handler(request: Request) -> Response:
        hub, p = await authenticate(request, write=write)
        body = await read_json(request, hub) if request.method in ("POST", "PUT", "PATCH") else None
        result = await run_in_threadpool(fn, hub, p, request, body)
        if isinstance(result, Response):
            return result
        return JSONResponse(result)

    return handler


def _wait(request: Request, hub: Hub) -> float:
    return v.wait_seconds(request.query_params.get("wait"), hub.settings.max_wait_seconds)


def polled(fetch: Callable[[Hub, Principal, Request], dict[str, Any]]) -> Callable[[Request], Awaitable[Response]]:
    """Read endpoint with optional long-poll (?wait=N) and compact text (?format=text)."""

    async def handler(request: Request) -> Response:
        hub, p = await authenticate(request, write=False)
        result = await messages.long_poll(
            lambda: fetch(hub, p, request), _wait(request, hub), hub.notifier, hub.settings.poll_interval
        )
        if request.query_params.get("format") == "text":
            return PlainTextResponse(messages.as_text(result))
        return JSONResponse(result)

    return handler


def q(request: Request) -> dict[str, Any]:
    return dict(request.query_params)


def path(request: Request, name: str) -> str:
    return str(request.path_params[name])


def task_id(request: Request) -> int:
    return v.integer(request.path_params["task_id"], "task_id", lo=1)


# -- unauthenticated -------------------------------------------------------------


async def health(_: Request) -> Response:
    return JSONResponse({"ok": True, "service": "agentlink", "version": __version__})


async def agent_guide(_: Request) -> Response:
    return PlainTextResponse((STATIC / "agent-guide.md").read_text("utf-8"), media_type="text/markdown; charset=utf-8")


async def github_webhook(request: Request) -> Response:
    hub = await run_in_threadpool(get_hub)
    raw = await request.body()
    if len(raw) > 5 * 1024 * 1024:  # GitHub caps payloads at 25 MB; we only need summaries
        raise TooLarge("webhook payload too large")
    github.verify_signature(hub.settings.github_webhook_secret, raw, request.headers.get("x-hub-signature-256"))
    try:
        payload = json.loads(raw)
    except ValueError:
        raise BadRequest("invalid JSON") from None
    event = request.headers.get("x-github-event", "")
    delivery = request.headers.get("x-github-delivery", "")[:64]
    result = await run_in_threadpool(github.ingest, hub.db, event, delivery, v.obj(payload), hub.notifier)
    return JSONResponse(result)


# -- export (streamed) -----------------------------------------------------------


def _export(h: Hub, p: Principal, r: Request, _: Any) -> Response:
    upto = v.integer(r.query_params.get("upto_id"), "upto_id", lo=1)
    p.require_admin()
    return StreamingResponse(archive.export_lines(h.db, p, upto), media_type="application/x-ndjson")


# -- route table -------------------------------------------------------------------


def W(fn: Handler) -> Callable[[Request], Awaitable[Response]]:
    return endpoint(fn, write=True)


def R(fn: Handler) -> Callable[[Request], Awaitable[Response]]:
    return endpoint(fn, write=False)


routes = [
    Route("/api/health", health),
    Route("/api/agent-guide", agent_guide),
    Route("/api/github/webhook", github_webhook, methods=["POST"]),
    Route("/api/me", R(lambda h, p, r, b: directory.whoami(h.db, p))),
    Route("/api/channels", R(lambda h, p, r, b: directory.list_channels(h.db, p))),
    Route("/api/channels", W(lambda h, p, r, b: directory.create_channel(h.db, p, v.obj(b))), methods=["POST"]),
    Route("/api/channels/{ch}", R(lambda h, p, r, b: directory.get_channel(h.db, p, path(r, "ch")))),
    Route(
        "/api/channels/{ch}/members",
        W(lambda h, p, r, b: directory.set_membership(h.db, p, path(r, "ch"), v.obj(b).get("agent"), True)),
        methods=["POST"],
    ),
    Route(
        "/api/channels/{ch}/members/{agent}",
        W(lambda h, p, r, b: directory.set_membership(h.db, p, path(r, "ch"), path(r, "agent"), False)),
        methods=["DELETE"],
    ),
    Route(
        "/api/channels/{ch}/messages", polled(lambda h, p, r: messages.channel_messages(h.db, p, path(r, "ch"), q(r)))
    ),
    Route(
        "/api/channels/{ch}/messages",
        W(lambda h, p, r, b: messages.post_message(h.db, p, path(r, "ch"), b, notifier=h.notifier)),
        methods=["POST"],
    ),
    Route("/api/feed", polled(lambda h, p, r: messages.feed(h.db, p, q(r)))),
    Route("/api/inbox", polled(lambda h, p, r: messages.inbox(h.db, p, q(r)))),
    Route("/api/channels/{ch}/tasks", R(lambda h, p, r, b: coordination.list_tasks(h.db, p, path(r, "ch"), q(r)))),
    Route(
        "/api/channels/{ch}/tasks",
        W(lambda h, p, r, b: coordination.create_task(h.db, p, path(r, "ch"), b, h.notifier)),
        methods=["POST"],
    ),
    Route("/api/tasks/{task_id}", R(lambda h, p, r, b: coordination.get_task(h.db, p, task_id(r)))),
    Route(
        "/api/tasks/{task_id}",
        W(lambda h, p, r, b: coordination.update_task(h.db, p, task_id(r), b, h.notifier)),
        methods=["PATCH"],
    ),
    Route(
        "/api/tasks/{task_id}/claim",
        W(lambda h, p, r, b: coordination.claim_task(h.db, p, task_id(r), h.notifier)),
        methods=["POST"],
    ),
    Route("/api/channels/{ch}/leases", R(lambda h, p, r, b: coordination.list_leases(h.db, p, path(r, "ch")))),
    Route(
        "/api/channels/{ch}/leases",
        W(lambda h, p, r, b: coordination.acquire_lease(h.db, p, path(r, "ch"), b, h.notifier)),
        methods=["POST"],
    ),
    Route(
        "/api/channels/{ch}/leases",
        W(
            lambda h, p, r, b: coordination.release_lease(
                h.db, p, path(r, "ch"), r.query_params.get("resource"), h.notifier
            )
        ),
        methods=["DELETE"],
    ),
    Route(
        "/api/channels/{ch}/kv",
        R(lambda h, p, r, b: coordination.kv_list(h.db, p, path(r, "ch"), r.query_params.get("prefix", ""))),
    ),
    Route(
        "/api/channels/{ch}/kv/{key:path}",
        R(lambda h, p, r, b: coordination.kv_get(h.db, p, path(r, "ch"), path(r, "key"))),
    ),
    Route(
        "/api/channels/{ch}/kv/{key:path}",
        W(lambda h, p, r, b: coordination.kv_set(h.db, p, path(r, "ch"), path(r, "key"), b, h.notifier)),
        methods=["PUT"],
    ),
    Route("/api/presence", R(lambda h, p, r, b: coordination.presence(h.db, p))),
    Route("/api/presence", W(lambda h, p, r, b: coordination.heartbeat(h.db, p, b)), methods=["POST"]),
    Route("/api/stats", R(lambda h, p, r, b: archive.stats(h.db, p))),
    # admin
    Route("/api/admin/agents", R(lambda h, p, r, b: directory.list_agents(h.db, p))),
    Route("/api/admin/agents", W(lambda h, p, r, b: directory.create_agent(h.db, p, v.obj(b))), methods=["POST"]),
    Route(
        "/api/admin/agents/{name}/tokens",
        W(lambda h, p, r, b: directory.issue_token(h.db, p, path(r, "name"), v.obj(b))),
        methods=["POST"],
    ),
    Route(
        "/api/admin/agents/{name}/disable",
        W(lambda h, p, r, b: directory.set_agent_disabled(h.db, p, path(r, "name"), True)),
        methods=["POST"],
    ),
    Route(
        "/api/admin/agents/{name}/enable",
        W(lambda h, p, r, b: directory.set_agent_disabled(h.db, p, path(r, "name"), False)),
        methods=["POST"],
    ),
    Route(
        "/api/admin/agents/{name}",
        W(
            lambda h, p, r, b: directory.delete_agent(
                h.db, p, path(r, "name"), r.query_params.get("purge_messages") in ("1", "true")
            )
        ),
        methods=["DELETE"],
    ),
    Route(
        "/api/admin/tokens/{token_id}",
        W(lambda h, p, r, b: directory.revoke_token(h.db, p, v.integer(r.path_params["token_id"], "token_id", lo=1))),
        methods=["DELETE"],
    ),
    Route(
        "/api/admin/messages/{message_id}",
        W(
            lambda h, p, r, b: messages.delete_message(
                h.db, p, v.integer(r.path_params["message_id"], "message_id", lo=1)
            )
        ),
        methods=["DELETE"],
    ),
    Route(
        "/api/admin/channels/{ch}",
        W(lambda h, p, r, b: directory.delete_channel(h.db, p, path(r, "ch"))),
        methods=["DELETE"],
    ),
    Route("/api/admin/audit", R(lambda h, p, r, b: directory.audit_log(h.db, p))),
    Route(
        "/api/admin/archive/plan", R(lambda h, p, r, b: archive.plan(h.db, p, r.query_params.get("older_than_days")))
    ),
    Route("/api/admin/archive/export", R(_export)),
    Route("/api/admin/archive/prune", W(lambda h, p, r, b: archive.prune(h.db, p, b)), methods=["POST"]),
]


async def app_error(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, AppError)
    headers = {}
    if exc.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if "retry_after" in exc.extra:
        headers["Retry-After"] = str(exc.extra["retry_after"])
    return JSONResponse(exc.to_dict(), status_code=exc.status, headers=headers)


async def internal_error(_: Request, exc: Exception) -> Response:
    log.exception("unhandled error", exc_info=exc)
    return JSONResponse({"error": "internal", "message": "internal server error"}, status_code=500)
