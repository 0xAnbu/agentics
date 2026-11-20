"""Input validation at the trust boundary. Everything from the network passes here."""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import BadRequest, TooLarge

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")

ROLES = ("admin", "member", "monitor")
# Kinds an agent may post. "event" (GitHub) and "system" (task/lease bookkeeping)
# are reserved for the server so they can be trusted by readers.
POSTABLE_KINDS = (
    "chat",
    "task",
    "status",
    "question",
    "answer",
    "decision",
    "handoff",
    "review",
    "alert",
    "context",
)
ALL_KINDS = (*POSTABLE_KINDS, "event", "system")
TASK_STATUSES = ("open", "claimed", "in_progress", "blocked", "review", "done", "cancelled")

MAX_BODY = 16_000
MAX_DATA = 32_000
MAX_LIST = 32


def obj(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise BadRequest("JSON object expected")
    return payload


def name(value: Any, field: str = "name") -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise BadRequest(f"{field} must match {NAME_RE.pattern}")
    return value


def text(value: Any, field: str, max_len: int, *, required: bool = False) -> str:
    if value is None:
        if required:
            raise BadRequest(f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise BadRequest(f"{field} must be a string")
    if required and not value.strip():
        raise BadRequest(f"{field} is required")
    if len(value) > max_len:
        raise TooLarge(f"{field} exceeds {max_len} characters")
    return value


def choice(value: Any, field: str, options: tuple[str, ...], default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if value not in options:
        raise BadRequest(f"{field} must be one of: {', '.join(options)}")
    return str(value)


def integer(value: Any, field: str, *, default: int | None = None, lo: int = 0, hi: int = 2**62) -> int:
    if value is None or value == "":
        if default is None:
            raise BadRequest(f"{field} is required")
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{field} must be an integer") from None
    if isinstance(value, bool) or n < lo or n > hi:
        raise BadRequest(f"{field} must be between {lo} and {hi}")
    return n


def wait_seconds(value: Any, ceiling: int) -> float:
    """Long-poll duration, clamped (not rejected) to the server ceiling so operators can
    lower AGENTLINK_MAX_WAIT without breaking clients that ask for more."""
    return float(min(integer(value, "wait", default=0, hi=3600), max(0, ceiling)))


def opt_int(value: Any, field: str) -> int | None:
    return None if value is None else integer(value, field, lo=1)


def str_list(value: Any, field: str, max_items: int = MAX_LIST, max_len: int = 200) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise BadRequest(f"{field} must be a list of at most {max_items} strings")
    out = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > max_len:
            raise BadRequest(f"{field} items must be non-empty strings <= {max_len} chars")
        out.append(item)
    return out


def json_blob(value: Any, field: str, max_len: int = MAX_DATA) -> str | None:
    if value is None:
        return None
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if len(encoded) > max_len:
        raise TooLarge(f"{field} exceeds {max_len} bytes once encoded")
    return encoded


REF_KEYS = {"repo", "branch", "commit", "pr", "issue", "path", "url", "line", "run"}


def refs(value: Any) -> list[dict[str, Any]]:
    """GitHub references: [{"repo": "o/r", "pr": 12}, {"path": "src/x.py", "line": 40}]."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise BadRequest(f"refs must be a list of at most {MAX_LIST} objects")
    out: list[dict[str, Any]] = []
    for ref in value:
        if not isinstance(ref, dict) or not ref or not set(ref) <= REF_KEYS:
            raise BadRequest(f"each ref must be an object with keys from {sorted(REF_KEYS)}")
        for k, v in ref.items():
            if not isinstance(v, (str, int)) or isinstance(v, bool) or len(str(v)) > 500:
                raise BadRequest(f"ref.{k} must be a short string or integer")
        if "repo" in ref and not REPO_RE.match(str(ref["repo"])):
            raise BadRequest("ref.repo must look like owner/name")
        out.append(ref)
    return out


def repo(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not REPO_RE.match(value):
        raise BadRequest("repo must look like owner/name")
    return value


def key(value: Any) -> str:
    if not isinstance(value, str) or not KEY_RE.match(value):
        raise BadRequest(f"key must match {KEY_RE.pattern}")
    return value


def resource(value: Any) -> str:
    """Lease resource: a repo path or a logical name, normalised (no leading './' or '/')."""
    v = text(value, "resource", 300, required=True).strip()
    while v.startswith(("./", "/")):
        v = v[2:] if v.startswith("./") else v[1:]
    if not v or ".." in v.split("/") or any(c in v for c in "\x00\n\r"):
        raise BadRequest("invalid resource")
    return v
