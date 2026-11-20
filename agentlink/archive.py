"""Retention without data loss: verified export, then prune; plus usage statistics.

Flow (driven by ``agentlink archive``):
  1. ``plan(older_than_days)``  -> highest message id older than the cutoff.
  2. ``export_lines(upto_id)``  -> canonical NDJSON, streamed; the client gzips and hashes it.
  3. ``prune(upto_id, count, sha256)`` -> the server recomputes count + hash from the
     DB and deletes ONLY if they match, so nothing is deleted that was not archived intact.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

from . import validation as v
from .db import Database
from .directory import audit
from .errors import Conflict
from .messages import _SELECT, _shape
from .security import Principal, now_ms

BATCH = 500
DAY_MS = 86_400_000


def plan(db: Database, p: Principal, older_than_days: Any) -> dict[str, Any]:
    p.require_admin()
    days = v.integer(older_than_days, "older_than_days", lo=0, hi=36500)
    cutoff = now_ms() - days * DAY_MS
    row = db.one("SELECT MAX(id) AS upto, COUNT(*) AS n FROM messages WHERE created_at < ?", (cutoff,))
    return {"upto_id": (row or {}).get("upto") or 0, "count": int((row or {}).get("n") or 0), "older_than_days": days}


def export_lines(db: Database, p: Principal, upto_id: int) -> Iterator[str]:
    p.require_admin()
    last = 0
    while True:
        rows = db.query(f"{_SELECT} WHERE m.id > ? AND m.id <= ? ORDER BY m.id LIMIT ?", (last, upto_id, BATCH))
        if not rows:
            return
        for msg in _shape(db, rows):
            yield json.dumps(msg, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
        last = int(rows[-1]["id"])


def digest(db: Database, p: Principal, upto_id: int) -> tuple[int, str]:
    h, n = hashlib.sha256(), 0
    for line in export_lines(db, p, upto_id):
        h.update(line.encode("utf-8"))
        n += 1
    return n, h.hexdigest()


def prune(db: Database, p: Principal, payload: dict[str, Any]) -> dict[str, Any]:
    p.require_admin()
    payload = v.obj(payload)
    upto = v.integer(payload.get("upto_id"), "upto_id", lo=1)
    count = v.integer(payload.get("count"), "count", lo=0)
    sha = v.text(payload.get("sha256"), "sha256", 64, required=True)
    actual_n, actual_sha = digest(db, p, upto)
    if (actual_n, actual_sha) != (count, sha):
        raise Conflict(
            "archive does not match database; nothing deleted", expected_count=actual_n, expected_sha256=actual_sha
        )
    with db.transaction() as tx:
        tx.execute("DELETE FROM mentions WHERE message_id <= ?", (upto,))
        deleted = tx.execute("DELETE FROM messages WHERE id <= ?", (upto,))
        tx.execute(
            "INSERT INTO archives(upto_id, count, sha256, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (upto, count, sha, p.agent_id, now_ms()),
        )
        audit(tx, p, "archive.prune", str(upto), f"count={count} sha256={sha}")
    if db.dialect == "sqlite":
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"deleted": deleted, "upto_id": upto, "sha256": sha}


def stats(db: Database, p: Principal) -> dict[str, Any]:
    if p.role not in ("admin", "monitor"):
        p.require_admin()
    now = now_ms()
    day = now - DAY_MS
    totals = (
        db.one(
            "SELECT COUNT(*) AS total, "
            "COALESCE(AVG(LENGTH(body) + COALESCE(LENGTH(data), 0) + COALESCE(LENGTH(refs), 0)), 0) AS avg_payload "
            "FROM messages"
        )
        or {}
    )
    last24 = db.one("SELECT COUNT(*) AS n FROM messages WHERE created_at >= ?", (day,)) or {}
    per_kind = db.query(
        "SELECT kind, COUNT(*) AS n FROM messages WHERE created_at >= ? GROUP BY kind ORDER BY n DESC", (day,)
    )
    per_agent = db.query(
        "SELECT a.name AS agent, COUNT(*) AS n FROM messages m JOIN agents a "
        "ON a.id = m.author_id WHERE m.created_at >= ? GROUP BY a.name ORDER BY n DESC",
        (day,),
    )
    hourly = db.query(
        "SELECT (created_at / 3600000) AS h, COUNT(*) AS n FROM messages WHERE created_at >= ? "
        "GROUP BY (created_at / 3600000) ORDER BY h",
        (day,),
    )
    size = db.size_bytes()
    total = int(totals.get("total") or 0)
    n24 = int(last24.get("n") or 0)
    bytes_per_msg = size / total if total else 0
    return {
        "messages_total": total,
        "messages_24h": n24,
        "avg_payload_chars": round(float(totals.get("avg_payload") or 0), 1),
        "db_bytes": size,
        "db_bytes_per_message": round(bytes_per_msg, 1),
        "projected_bytes_per_day": round(bytes_per_msg * n24),
        "by_kind_24h": per_kind,
        "by_agent_24h": per_agent,
        "hourly_24h": [{"hour_ms": int(r["h"]) * 3600000, "n": r["n"]} for r in hourly],
        "dialect": db.dialect,
        "archives": db.query("SELECT upto_id, count, sha256, created_at FROM archives ORDER BY id DESC LIMIT 10"),
    }
