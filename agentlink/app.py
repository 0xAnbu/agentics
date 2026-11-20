"""ASGI application: REST API, MCP endpoint, monitoring UI and security headers."""

from __future__ import annotations

import logging
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api import STATIC, app_error, internal_error, routes
from .errors import AppError
from .mcp import mcp_endpoint

logging.basicConfig(
    level=os.environ.get("AGENTLINK_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

SECURITY_HEADERS = [
    (
        b"content-security-policy",
        b"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
        b"base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
]


class SecurityHeaders:
    """Pure ASGI middleware (safe with streaming responses)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_api = scope["path"].startswith(("/api", "/mcp"))

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(SECURITY_HEADERS)
                if is_api:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)


async def index(_: Request) -> Response:
    return FileResponse(STATIC / "index.html")


def create_app() -> Starlette:
    app = Starlette(
        routes=[
            *routes,
            Route("/mcp", mcp_endpoint, methods=["GET", "POST", "DELETE"]),
            Route("/", index),
            Mount("/static", app=StaticFiles(directory=STATIC), name="static"),
        ],
        exception_handlers={AppError: app_error, Exception: internal_error},
    )
    app.add_middleware(SecurityHeaders)
    return app


app = create_app()
