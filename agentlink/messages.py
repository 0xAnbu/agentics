"""Messages: posting, incremental reads (cursor + long-poll), feed, inbox and formatting."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from starlette.concurrency import run_in_threadpool

from . import validation as v
from .db import Database, Executor, Row
from .directory import audit, channel_for, visible_channel_ids_sql
from .errors import BadRequest, NotFound
from .security import Principal, now_ms

MENTION_RE = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9._-]{0,63})")
MAX_LIMIT = 200


class Notifier:
    """In-process wake-up for long-polls. Cross-instance delivery (serverless)
    falls back to periodic DB re-checks, so this is an optimisation only."""

    def __init__(self) -> None:
        self._version = 0
        self._lock = threading.Lock()

    @property
    def version(self) -> int:
        return self._version

    def bump(self) -> None:
        with self._lock:
            self._version += 1

    async def wait_change(self, seen: int, timeout: float) -> None:
        end = time.monotonic() + timeout
        while self._version == seen and time.monotonic() < end:
            await asyncio.sleep(0.1)


async def long_poll(fetch: Callable[[], Any], wait: float, notifier: Notifier, interval: float) -> Any:
    """Run ``fetch`` until it returns something truthy or ``wait`` seconds pass."""
    deadline = time.monotonic() + max(0.0, wait)
    seen = notifier.version
    result = await run_in_threadpool(fetch)
    while not _has_items(result) and time.monotonic() < deadline:
        await notifier.wait_change(seen, min(interval, deadline - time.monotonic()))
        seen = notifier.version
        result = await run_in_threadpool(fetch)
    return result


def _has_items(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("messages"))
    return bool(result)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# -- write path ----------------------------------------------------------------


def delete_message(db: Database, p: Principal, message_id: int) -> dict[str, Any]:
    """Admin-only moderation: remove one message. Replies keep their (now dangling) reply_to."""
    p.require_admin()
    with db.transaction() as tx:
        row = tx.one(
            "SELECT m.id, c.name AS channel, a.name AS author FROM messages m "
            "JOIN channels c ON c.id = m.channel_id JOIN agents a ON a.id = m.author_id WHERE m.id = ?",
            (message_id,),
        )
        if row is None:
            raise NotFound(f"message {message_id} not found")
        tx.execute("DELETE FROM mentions WHERE message_id = ?", (message_id,))
        tx.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        audit(tx, p, "message.delete", str(message_id), f"channel={row['channel']} author={row['author']}")
    return {"deleted": message_id, "channel": row["channel"]}


def post_message(
    db: Database,
    p: Principal,
    channel: str | int,
    payload: dict[str, Any],
    *,
    system: bool = False,
    notifier: Notifier | None = None,
) -> dict[str, Any]:
    payload = v.obj(payload)
    ch = channel_for(db, p, channel, write=True)
    kinds = v.ALL_KINDS if system else v.POSTABLE_KINDS
    kind = v.choice(payload.get("kind"), "kind", kinds, default="chat")
    body = v.text(payload.get("body"), "body", v.MAX_BODY, required=True)
    data = v.json_blob(payload.get("data"), "data")
    refs = v.refs(payload.get("refs"))
    tags = v.str_list(payload.get("tags"), "tags", max_len=50)
    reply_to = v.opt_int(payload.get("reply_to"), "reply_to")
    client_id = v.text(payload.get("client_id"), "client_id", 100) or None
    mention_names = set(v.str_list(payload.get("mentions"), "mentions", max_len=64))
    mention_names |= set(MENTION_RE.findall(body))

    with db.transaction() as tx:
        if client_id:
            dup = tx.one("SELECT id FROM messages WHERE author_id = ? AND client_id = ?", (p.agent_id, client_id))
            if dup:  # idempotent retry: return the original
                return {**_load(tx, [int(dup["id"])])[0], "duplicate": True}
        thread_id = None
        if reply_to:
            parent = tx.one("SELECT id, channel_id, thread_id FROM messages WHERE id = ?", (reply_to,))
            if parent is None or parent["channel_id"] != ch["id"]:
                raise BadRequest("reply_to must reference a message in the same channel")
            thread_id = parent["thread_id"] or parent["id"]
        row = tx.one(
            "INSERT INTO messages(channel_id, author_id, kind, body, data, refs, tags, thread_id, reply_to, "
            "client_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                ch["id"],
                p.agent_id,
                kind,
                body,
                data,
                json.dumps(refs) if refs else None,
                json.dumps(tags) if tags else None,
                thread_id,
                reply_to,
                client_id,
                now_ms(),
            ),
        )
        assert row is not None
        mid = int(row["id"])
        if mention_names:
            placeholders = ",".join("?" * len(mention_names))
            for a in tx.query(f"SELECT id FROM agents WHERE name IN ({placeholders})", tuple(mention_names)):
                if int(a["id"]) != p.agent_id:
                    tx.execute("INSERT INTO mentions(agent_id, message_id) VALUES (?, ?)", (a["id"], mid))
        msg = _load(tx, [mid])[0]
    if notifier:
        notifier.bump()
    return msg


# -- read path -------------------------------------------------------------------

_SELECT = (
    "SELECT m.id, m.kind, m.body, m.data, m.refs, m.tags, m.thread_id, m.reply_to, m.created_at, "
    "c.name AS channel, a.name AS author FROM messages m "
    "JOIN channels c ON c.id = m.channel_id JOIN agents a ON a.id = m.author_id "
)


def _load(db: Executor, ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    return _shape(db, db.query(f"{_SELECT} WHERE m.id IN ({ph}) ORDER BY m.id", tuple(ids)))


def _shape(db: Executor, rows: list[Row]) -> list[dict[str, Any]]:
    if not rows:
        return []
    ph = ",".join("?" * len(rows))
    mentions: dict[int, list[str]] = {}
    for r in db.query(
        f"SELECT mn.message_id, a.name FROM mentions mn JOIN agents a ON a.id = mn.agent_id "
        f"WHERE mn.message_id IN ({ph})",
        tuple(int(r["id"]) for r in rows),
    ):
        mentions.setdefault(int(r["message_id"]), []).append(str(r["name"]))
    out = []
    for r in rows:
        item: dict[str, Any] = {
            "id": r["id"],
            "channel": r["channel"],
            "author": r["author"],
            "kind": r["kind"],
            "body": r["body"],
            "ts": iso(int(r["created_at"])),
        }
        for k in ("data", "refs", "tags"):
            if r[k]:
                item[k] = json.loads(r[k])
        for k in ("thread_id", "reply_to"):
            if r[k]:
                item[k] = r[k]
        if int(r["id"]) in mentions:
            item["mentions"] = sorted(mentions[int(r["id"])])
        out.append(item)
    return out


def _query_messages(db: Database, where: str, params: tuple[Any, ...], after: int | None, limit: int) -> dict[str, Any]:
    if after is None:  # first read: newest `limit` messages, returned oldest-first
        rows = db.query(f"{_SELECT} WHERE {where} ORDER BY m.id DESC LIMIT ?", (*params, limit))
        rows.reverse()
    else:
        rows = db.query(f"{_SELECT} WHERE {where} AND m.id > ? ORDER BY m.id LIMIT ?", (*params, after, limit))
    msgs = _shape(db, rows)
    next_after = msgs[-1]["id"] if msgs else (after or 0)
    if after is None and not msgs:
        top = db.one(f"SELECT MAX(m.id) AS x FROM messages m WHERE {where}", params)
        next_after = (top or {}).get("x") or 0
    return {"messages": msgs, "next_after": next_after, "has_more": len(msgs) == limit}


def read_params(q: dict[str, Any]) -> tuple[int | None, int]:
    after = q.get("after")
    return (
        None if after in (None, "") else v.integer(after, "after"),
        v.integer(q.get("limit"), "limit", default=50, lo=1, hi=MAX_LIMIT),
    )


def channel_messages(db: Database, p: Principal, channel: str | int, q: dict[str, Any]) -> dict[str, Any]:
    ch = channel_for(db, p, channel)
    after, limit = read_params(q)
    where, params = "m.channel_id = ?", [ch["id"]]
    if q.get("thread"):
        tid = v.integer(q.get("thread"), "thread", lo=1)
        where += " AND (m.thread_id = ? OR m.id = ?)"
        params += [tid, tid]
    kinds = [k for k in str(q.get("kinds") or "").split(",") if k]
    if kinds:
        for k in kinds:
            v.choice(k, "kinds", v.ALL_KINDS)
        where += f" AND m.kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    return _query_messages(db, where, tuple(params), after, limit)


def feed(db: Database, p: Principal, q: dict[str, Any]) -> dict[str, Any]:
    after, limit = read_params(q)
    where, params = visible_channel_ids_sql(p, "m.channel_id")
    return _query_messages(db, where, params, after, limit)


def inbox(db: Database, p: Principal, q: dict[str, Any]) -> dict[str, Any]:
    """Messages that mention me, plus my open tasks. The cursor covers mentions only."""
    after, limit = read_params(q)
    where, params = visible_channel_ids_sql(p, "m.channel_id")
    where = "m.id IN (SELECT mn.message_id FROM mentions mn WHERE mn.agent_id = ?) AND " + where
    res = _query_messages(db, where, (p.agent_id, *params), 0 if after is None else after, limit)
    res["open_tasks"] = db.query(
        "SELECT t.id, c.name AS channel, t.title, t.status, t.priority FROM tasks t "
        "JOIN channels c ON c.id = t.channel_id WHERE t.assignee_id = ? "
        "AND t.status NOT IN ('done', 'cancelled') ORDER BY t.priority, t.id",
        (p.agent_id,),
    )
    return res


# -- compact text rendering (token-efficient for LLM agents) -----------------------


def as_text(result: dict[str, Any]) -> str:
    lines = []
    for m in result.get("messages", []):
        head = f"#{m['id']} {m['ts'][5:16]} {m['channel']} @{m['author']} [{m['kind']}]"
        if m.get("reply_to"):
            head += f" re#{m['reply_to']}"
        if m.get("mentions"):
            head += " ->" + ",".join("@" + x for x in m["mentions"])
        if m.get("tags"):
            head += " " + " ".join("#" + t for t in m["tags"])
        lines.append(f"{head}: {m['body']}")
        if m.get("refs"):
            lines.append("  refs: " + json.dumps(m["refs"], separators=(",", ":")))
        if m.get("data"):
            lines.append("  data: " + json.dumps(m["data"], separators=(",", ":"), ensure_ascii=False))
    for t in result.get("open_tasks", []):
        lines.append(f"task #{t['id']} {t['channel']} [{t['status']}] p{t['priority']}: {t['title']}")
    lines.append(f"next_after={result.get('next_after', 0)}" + (" has_more" if result.get("has_more") else ""))
    return "\n".join(lines) + "\n"
