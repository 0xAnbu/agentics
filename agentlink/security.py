"""Credentials, authentication and rate limiting.

Token design: ``al_`` + 32 random bytes (base64url). Only SHA-256(token) is stored.
A slow password hash (Argon2) is unnecessary here because tokens are
256-bit random secrets, not human passwords: brute force is infeasible and a
fast hash keeps every request cheap. Lookups go through the hash index.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from .db import Database
from .errors import Forbidden, RateLimited, Unauthorized

TOKEN_PREFIX = "al_"  # noqa: S105 - a prefix, not a secret


def now_ms() -> int:
    return int(time.time() * 1000)


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    agent_id: int
    name: str
    role: str  # admin | member | monitor
    token_id: int | None  # None => env admin token

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def require_admin(self) -> None:
        if not self.is_admin:
            raise Forbidden("admin role required")

    def require_writer(self) -> None:
        if self.role == "monitor":
            raise Forbidden("monitor tokens are read-only")


ADMIN_AGENT = "admin"


def ensure_admin_agent(db: Database) -> int:
    row = db.one("SELECT id FROM agents WHERE name = ?", (ADMIN_AGENT,))
    if row:
        return int(row["id"])
    db.execute(
        "INSERT INTO agents(name, role, description, created_at) VALUES (?, 'admin', ?, ?) "
        "ON CONFLICT (name) DO NOTHING",
        (ADMIN_AGENT, "Bootstrap administrator (AGENTLINK_ADMIN_TOKEN)", now_ms()),
    )
    row = db.one("SELECT id FROM agents WHERE name = ?", (ADMIN_AGENT,))
    assert row is not None
    return int(row["id"])


class Authenticator:
    def __init__(self, db: Database, admin_token: str) -> None:
        self.db = db
        self._admin_hash = hash_token(admin_token) if admin_token else ""
        self._admin_id: int | None = None
        self._touch: dict[int, float] = {}  # token_id -> last last_used_at write

    def authenticate(self, header: str | None) -> Principal:
        if not header or not header.startswith("Bearer "):
            raise Unauthorized("missing bearer token")
        token = header[7:].strip()
        if not token.startswith(TOKEN_PREFIX) or len(token) > 200:
            raise Unauthorized("invalid token")
        digest = hash_token(token)
        if self._admin_hash and hmac.compare_digest(digest, self._admin_hash):
            if self._admin_id is None:
                self._admin_id = ensure_admin_agent(self.db)
            return Principal(self._admin_id, ADMIN_AGENT, "admin", None)
        row = self.db.one(
            "SELECT t.id AS tid, t.expires_at, t.revoked_at, a.id AS aid, a.name, a.role, a.disabled "
            "FROM tokens t JOIN agents a ON a.id = t.agent_id WHERE t.token_hash = ?",
            (digest,),
        )
        now = now_ms()
        if (
            row is None
            or row["revoked_at"] is not None
            or row["disabled"]
            or (row["expires_at"] is not None and row["expires_at"] <= now)
        ):
            raise Unauthorized("invalid token")
        tid = int(row["tid"])
        # last_used_at is informational; write it at most once a minute per token.
        if time.monotonic() - self._touch.get(tid, 0) > 60:
            self._touch[tid] = time.monotonic()
            self.db.execute("UPDATE tokens SET last_used_at = ? WHERE id = ?", (now, tid))
        return Principal(int(row["aid"]), str(row["name"]), str(row["role"]), tid)


class RateLimiter:
    """Writes: fixed 60 s window stored in the DB, so it holds across serverless
    instances. Reads: in-memory per process (best effort, cheap)."""

    def __init__(self, db: Database, writes_per_min: int, reads_per_min: int) -> None:
        self.db = db
        self.writes = writes_per_min
        self.reads = reads_per_min
        self._mem: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def check_write(self, p: Principal) -> None:
        if self.writes <= 0:
            return
        win = int(time.time() // 60)
        row = self.db.one(
            "INSERT INTO ratelimit(bucket, win, count) VALUES (?, ?, 1) "
            "ON CONFLICT (bucket) DO UPDATE SET "
            "count = CASE WHEN ratelimit.win = excluded.win THEN ratelimit.count + 1 ELSE 1 END, "
            "win = excluded.win RETURNING count",
            (f"w:{p.agent_id}", win),
        )
        if row and int(row["count"]) > self.writes:
            raise RateLimited("write rate limit exceeded", retry_after=60 - int(time.time()) % 60)

    def check_read(self, p: Principal) -> None:
        if self.reads <= 0:
            return
        win = int(time.time() // 60)
        key = f"r:{p.agent_id}"
        with self._lock:
            w, c = self._mem.get(key, (win, 0))
            c = c + 1 if w == win else 1
            self._mem[key] = (win, c)
        if c > self.reads:
            raise RateLimited("read rate limit exceeded", retry_after=60 - int(time.time()) % 60)
