"""Process-wide wiring: settings + database + auth, created lazily.

Lazy (first request) instead of at import/lifespan time because serverless
platforms may not run ASGI lifespan events and must not crash on import.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from .config import Settings
from .db import Database, migrate
from .errors import Unavailable
from .messages import Notifier
from .security import Authenticator, RateLimiter, ensure_admin_agent

log = logging.getLogger("agentlink")


@dataclass
class Hub:
    settings: Settings
    db: Database
    auth: Authenticator
    limiter: RateLimiter
    notifier: Notifier


_hub: Hub | None = None
_lock = threading.Lock()


def build_hub(settings: Settings) -> Hub:
    if settings.on_vercel and not settings.database_url:
        raise Unavailable(
            "No database configured. In Vercel: Storage -> Create Database -> Neon, connect it to this "
            "project and redeploy (it sets DATABASE_URL)."
        )
    db = Database(settings.database_url, settings.sqlite_path)
    migrate(db)
    ensure_admin_agent(db)
    if not settings.admin_token:
        log.warning("AGENTLINK_ADMIN_TOKEN is not set: no one can administer this instance")
    return Hub(
        settings=settings,
        db=db,
        auth=Authenticator(db, settings.admin_token),
        limiter=RateLimiter(db, settings.write_rate_per_min, settings.read_rate_per_min),
        notifier=Notifier(),
    )


def get_hub() -> Hub:
    global _hub
    if _hub is None:
        with _lock:
            if _hub is None:
                _hub = build_hub(Settings.from_env())
    return _hub


def set_hub(hub: Hub | None) -> None:
    """Test hook: install a hub built from explicit settings."""
    global _hub
    _hub = hub
