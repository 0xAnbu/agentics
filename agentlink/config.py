"""Runtime configuration, read once from environment variables.

Every knob has a safe default so `make dev` works with zero configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    admin_token: str = ""
    database_url: str = ""  # postgres://... ; empty => SQLite
    sqlite_path: str = "data/agentlink.db"
    github_webhook_secret: str = ""
    max_wait_seconds: int = 25  # long-poll ceiling (keep < platform timeout)
    poll_interval: float = 1.0  # DB re-check period during long-poll
    write_rate_per_min: int = 240  # per token, all mutating calls
    read_rate_per_min: int = 1200  # per token per instance, best effort
    max_body_bytes: int = 256 * 1024
    allowed_origins: tuple[str, ...] = field(default_factory=tuple)
    on_vercel: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        db_url = (
            os.environ.get("AGENTLINK_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
            or os.environ.get("POSTGRES_URL")
            or ""
        ).strip()
        origins = tuple(
            o.strip().rstrip("/") for o in os.environ.get("AGENTLINK_ALLOWED_ORIGINS", "").split(",") if o.strip()
        )
        return cls(
            admin_token=os.environ.get("AGENTLINK_ADMIN_TOKEN", "").strip(),
            database_url=db_url,
            sqlite_path=os.environ.get("AGENTLINK_DB_PATH", "data/agentlink.db"),
            github_webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip(),
            max_wait_seconds=_int("AGENTLINK_MAX_WAIT", 25),
            poll_interval=_float("AGENTLINK_POLL_INTERVAL", 2.0 if db_url else 1.0),
            write_rate_per_min=_int("AGENTLINK_WRITE_RATE", 240),
            read_rate_per_min=_int("AGENTLINK_READ_RATE", 1200),
            max_body_bytes=_int("AGENTLINK_MAX_BODY", 256 * 1024),
            allowed_origins=origins,
            on_vercel=bool(os.environ.get("VERCEL")),
        )
