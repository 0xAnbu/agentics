"""Domain errors. Services raise these; transports (REST, MCP) translate them."""

from __future__ import annotations


class AppError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, **extra: object) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict[str, object]:
        return {"error": self.code, "message": self.message, **self.extra}


class BadRequest(AppError):
    status, code = 400, "bad_request"


class Unauthorized(AppError):
    status, code = 401, "unauthorized"


class Forbidden(AppError):
    status, code = 403, "forbidden"


class NotFound(AppError):
    status, code = 404, "not_found"


class Conflict(AppError):
    status, code = 409, "conflict"


class TooLarge(AppError):
    status, code = 413, "payload_too_large"


class RateLimited(AppError):
    status, code = 429, "rate_limited"


class Unavailable(AppError):
    status, code = 503, "unavailable"
