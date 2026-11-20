"""Coordination primitives that stop agents stepping on each other:
tasks with atomic claims, path leases with TTL, shared versioned key-value
context, and presence heartbeats.

State changes also emit a ``system`` message in the channel, so an agent that
only follows the message stream still sees who claimed or locked what.
"""

from __future__ import annotations

import json
from typing import Any

from . import validation as v
from .db import Database, Row
from .directory import agent_by_name, channel_for
from .errors import BadRequest, Conflict, Forbidden, NotFound
from .messages import Notifier, iso, post_message
from .security import Principal, now_ms

ONLINE_WINDOW_MS = 120_000


def _sys(
    db: Database,
    p: Principal,
    channel_id: int,
    body: str,
    data: dict[str, Any],
    notifier: Notifier | None,
    mentions: list[str] | None = None,
) -> None:
    post_message(
        db,
        p,
        channel_id,
        {"kind": "system", "body": body, "data": data, "mentions": mentions},
        system=True,
        notifier=notifier,
    )


# -- tasks -----------------------------------------------------------------------


def _task_out(r: Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "channel": r["channel"],
        "title": r["title"],
        "description": r["description"],
        "status": r["status"],
        "priority": r["priority"],
        "assignee": r["assignee"],
        "created_by": r["creator"],
        "refs": json.loads(r["refs"]) if r["refs"] else [],
        "version": r["version"],
        "updated": iso(int(r["updated_at"])),
    }


_TASK_SELECT = (
    "SELECT t.*, c.name AS channel, a.name AS assignee, cr.name AS creator FROM tasks t "
    "JOIN channels c ON c.id = t.channel_id LEFT JOIN agents a ON a.id = t.assignee_id "
    "JOIN agents cr ON cr.id = t.created_by "
)


def get_task(db: Database, p: Principal, task_id: int) -> dict[str, Any]:
    row = db.one(f"{_TASK_SELECT} WHERE t.id = ?", (task_id,))
    if row is None:
        raise NotFound(f"task #{task_id} not found")
    channel_for(db, p, int(row["channel_id"]))
    return _task_out(row)


def create_task(
    db: Database, p: Principal, channel: str | int, payload: dict[str, Any], notifier: Notifier | None = None
) -> dict[str, Any]:
    payload = v.obj(payload)
    ch = channel_for(db, p, channel, write=True)
    title = v.text(payload.get("title"), "title", 200, required=True)
    desc = v.text(payload.get("description"), "description", v.MAX_BODY)
    prio = v.integer(payload.get("priority"), "priority", default=2, lo=1, hi=3)
    refs = v.refs(payload.get("refs"))
    assignee_id = None
    assignee = payload.get("assignee")
    if assignee:
        a = agent_by_name(db, v.name(assignee, "assignee"))
        assignee_id = int(a["id"])
    now = now_ms()
    row = db.one(
        "INSERT INTO tasks(channel_id, title, description, status, priority, assignee_id, created_by, refs, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (
            ch["id"],
            title,
            desc,
            "claimed" if assignee_id else "open",
            prio,
            assignee_id,
            p.agent_id,
            json.dumps(refs) if refs else None,
            now,
            now,
        ),
    )
    assert row is not None
    task = get_task(db, p, int(row["id"]))
    post_message(
        db,
        p,
        int(ch["id"]),
        {
            "kind": "task",
            "body": f"task #{task['id']} (p{prio}): {title}",
            "data": {"task_id": task["id"], "event": "created", "status": task["status"]},
            "refs": refs or None,
            "mentions": [assignee] if assignee else None,
        },
        notifier=notifier,
    )
    return task


def list_tasks(db: Database, p: Principal, channel: str | int, q: dict[str, Any]) -> list[dict[str, Any]]:
    ch = channel_for(db, p, channel)
    where, params = "t.channel_id = ?", [ch["id"]]
    status = q.get("status")
    if status:
        statuses = [v.choice(s, "status", v.TASK_STATUSES) for s in str(status).split(",")]
        where += f" AND t.status IN ({','.join('?' * len(statuses))})"
        params += statuses
    elif not q.get("all"):
        where += " AND t.status NOT IN ('done', 'cancelled')"
    if q.get("assignee"):
        where += " AND a.name = ?"
        params.append(v.name(q["assignee"], "assignee"))
    rows = db.query(f"{_TASK_SELECT} WHERE {where} ORDER BY t.priority, t.id LIMIT 200", tuple(params))
    return [_task_out(r) for r in rows]


def claim_task(db: Database, p: Principal, task_id: int, notifier: Notifier | None = None) -> dict[str, Any]:
    """Atomic: exactly one concurrent claimer wins; the others get 409 with the winner."""
    task = get_task(db, p, task_id)
    p.require_writer()
    ch = channel_for(db, p, task["channel"], write=True)
    n = db.execute(
        "UPDATE tasks SET status = 'claimed', assignee_id = ?, version = version + 1, updated_at = ? "
        "WHERE id = ? AND status = 'open'",
        (p.agent_id, now_ms(), task_id),
    )
    if not n:
        current = get_task(db, p, task_id)
        if current["assignee"] == p.name:
            return current
        raise Conflict(
            f"task #{task_id} is {current['status']}", assignee=current["assignee"], status=current["status"]
        )
    _sys(
        db,
        p,
        int(ch["id"]),
        f"@{p.name} claimed task #{task_id}: {task['title']}",
        {"task_id": task_id, "event": "claimed"},
        notifier,
    )
    return get_task(db, p, task_id)


def update_task(
    db: Database, p: Principal, task_id: int, payload: dict[str, Any], notifier: Notifier | None = None
) -> dict[str, Any]:
    payload = v.obj(payload)
    task = get_task(db, p, task_id)
    ch = channel_for(db, p, task["channel"], write=True)
    if not (p.is_admin or p.name in (task["assignee"], task["created_by"])):
        raise Forbidden("only the assignee, the creator or an admin can update this task")
    sets: list[str] = ["version = version + 1", "updated_at = ?"]
    params: list[Any] = [now_ms()]
    changes: dict[str, Any] = {}
    if "status" in payload:
        changes["status"] = v.choice(payload["status"], "status", v.TASK_STATUSES)
    if "assignee" in payload:
        if payload["assignee"] in (None, ""):
            sets.append("assignee_id = NULL")
            changes["assignee"] = None
            changes.setdefault("status", "open")
        else:
            a = agent_by_name(db, v.name(payload["assignee"], "assignee"))
            sets.append("assignee_id = ?")
            params.append(a["id"])
            changes["assignee"] = a["name"]
    for f, limit in (("title", 200), ("description", v.MAX_BODY)):
        if f in payload:
            sets.append(f"{f} = ?")
            params.append(v.text(payload[f], f, limit, required=(f == "title")))
            changes[f] = "updated"
    if "priority" in payload:
        sets.append("priority = ?")
        params.append(v.integer(payload["priority"], "priority", lo=1, hi=3))
        changes["priority"] = params[-1]
    if "status" in changes:
        sets.append("status = ?")
        params.append(changes["status"])
    where = "id = ?"
    wparams: list[Any] = [task_id]
    if payload.get("if_version") is not None:
        where += " AND version = ?"
        wparams.append(v.integer(payload["if_version"], "if_version", lo=1))
    if not changes:
        return task
    if not db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE {where}", (*params, *wparams)):
        raise Conflict("task changed since you read it", current=get_task(db, p, task_id))
    note = v.text(payload.get("note"), "note", 2000)
    summary = ", ".join(f"{k}={val}" for k, val in changes.items())
    body = f"@{p.name} updated task #{task_id} ({summary})" + (f": {note}" if note else "")
    mentions = [x for x in (changes.get("assignee"), task["created_by"]) if x and x != p.name]
    _sys(db, p, int(ch["id"]), body, {"task_id": task_id, "event": "updated", **changes}, notifier, mentions)
    return get_task(db, p, task_id)


# -- leases ----------------------------------------------------------------------------


def _norm(res: str) -> str:
    for suffix in ("/**", "/*"):
        if res.endswith(suffix):
            res = res[: -len(suffix)]
    return res.rstrip("/")


def overlaps(a: str, b: str) -> bool:
    """Path-aware overlap: 'src/auth' conflicts with 'src/auth/x.py' but not 'src/authz'."""
    a, b = _norm(a), _norm(b)
    return a == b or b.startswith(a + "/") or a.startswith(b + "/") or a == "" or b == ""


def _lease_out(r: Row) -> dict[str, Any]:
    return {
        "resource": r["resource"],
        "holder": r["holder"],
        "note": r["note"],
        "expires": iso(int(r["expires_at"])),
        "channel": r["channel"],
    }


_LEASE_SELECT = (
    "SELECT l.*, a.name AS holder, c.name AS channel FROM leases l "
    "JOIN agents a ON a.id = l.holder_id JOIN channels c ON c.id = l.channel_id "
)


def acquire_lease(
    db: Database, p: Principal, channel: str | int, payload: dict[str, Any], notifier: Notifier | None = None
) -> dict[str, Any]:
    payload = v.obj(payload)
    ch = channel_for(db, p, channel, write=True)
    res = v.resource(payload.get("resource"))
    ttl = v.integer(payload.get("ttl_seconds"), "ttl_seconds", default=900, lo=10, hi=86_400)
    note = v.text(payload.get("note"), "note", 300)
    now = now_ms()
    renewed = False
    with db.transaction() as tx:
        tx.execute("DELETE FROM leases WHERE channel_id = ? AND expires_at <= ?", (ch["id"], now))
        for other in tx.query(f"{_LEASE_SELECT} WHERE l.channel_id = ?", (ch["id"],)):
            if not overlaps(other["resource"], res):
                continue
            if int(other["holder_id"]) == p.agent_id and other["resource"] == res:
                tx.execute(
                    "UPDATE leases SET expires_at = ?, note = ? WHERE id = ?",
                    (now + ttl * 1000, note or other["note"], other["id"]),
                )
                renewed = True
                break
            if int(other["holder_id"]) != p.agent_id:
                raise Conflict(
                    f"'{res}' overlaps lease '{other['resource']}' held by @{other['holder']}", lease=_lease_out(other)
                )
        if not renewed:
            tx.execute(
                "INSERT INTO leases(channel_id, resource, holder_id, note, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ch["id"], res, p.agent_id, note, now + ttl * 1000, now),
            )
        row = tx.one(f"{_LEASE_SELECT} WHERE l.channel_id = ? AND l.resource = ?", (ch["id"], res))
    assert row is not None
    out = {**_lease_out(row), "renewed": renewed}
    if not renewed:
        _sys(
            db,
            p,
            int(ch["id"]),
            f"@{p.name} leased {res} for {ttl}s" + (f": {note}" if note else ""),
            {"event": "lease.acquired", "resource": res, "ttl_seconds": ttl},
            notifier,
        )
    return out


def release_lease(
    db: Database, p: Principal, channel: str | int, resource: Any, notifier: Notifier | None = None
) -> dict[str, Any]:
    ch = channel_for(db, p, channel, write=True)
    res = v.resource(resource)
    row = db.one("SELECT holder_id FROM leases WHERE channel_id = ? AND resource = ?", (ch["id"], res))
    if row is None:
        raise NotFound(f"no lease on '{res}'")
    if int(row["holder_id"]) != p.agent_id and not p.is_admin:
        raise Forbidden("only the holder or an admin can release this lease")
    db.execute("DELETE FROM leases WHERE channel_id = ? AND resource = ?", (ch["id"], res))
    _sys(db, p, int(ch["id"]), f"@{p.name} released {res}", {"event": "lease.released", "resource": res}, notifier)
    return {"released": res}


def list_leases(db: Database, p: Principal, channel: str | int) -> list[dict[str, Any]]:
    ch = channel_for(db, p, channel)
    rows = db.query(
        f"{_LEASE_SELECT} WHERE l.channel_id = ? AND l.expires_at > ? ORDER BY l.resource", (ch["id"], now_ms())
    )
    return [_lease_out(r) for r in rows]


# -- shared key-value context ---------------------------------------------------------------


def kv_get(db: Database, p: Principal, channel: str | int, key: Any) -> dict[str, Any]:
    ch = channel_for(db, p, channel)
    k = v.key(key)
    row = db.one(
        "SELECT kv.*, a.name AS updated_by_name FROM kv JOIN agents a ON a.id = kv.updated_by "
        "WHERE channel_id = ? AND key = ?",
        (ch["id"], k),
    )
    if row is None:
        raise NotFound(f"key '{k}' not set")
    return {
        "key": k,
        "value": json.loads(row["value"]),
        "version": row["version"],
        "updated_by": row["updated_by_name"],
        "updated": iso(int(row["updated_at"])),
    }


def kv_list(db: Database, p: Principal, channel: str | int, prefix: str = "") -> list[dict[str, Any]]:
    ch = channel_for(db, p, channel)
    rows = db.query(
        "SELECT kv.key, kv.value, kv.version, kv.updated_at, a.name AS updated_by FROM kv "
        "JOIN agents a ON a.id = kv.updated_by WHERE channel_id = ? ORDER BY key",
        (ch["id"],),
    )
    return [
        {
            "key": r["key"],
            "value": json.loads(r["value"]),
            "version": r["version"],
            "updated_by": r["updated_by"],
            "updated": iso(int(r["updated_at"])),
        }
        for r in rows
        if str(r["key"]).startswith(prefix)
    ]


def kv_set(
    db: Database, p: Principal, channel: str | int, key: Any, payload: dict[str, Any], notifier: Notifier | None = None
) -> dict[str, Any]:
    payload = v.obj(payload)
    ch = channel_for(db, p, channel, write=True)
    k = v.key(key)
    if "value" not in payload:
        raise BadRequest("value is required")
    value = v.json_blob(payload["value"], "value")
    expected = payload.get("if_version")
    now = now_ms()
    with db.transaction() as tx:
        row = tx.one("SELECT version FROM kv WHERE channel_id = ? AND key = ?", (ch["id"], k))
        current = int(row["version"]) if row else 0
        if expected is not None and v.integer(expected, "if_version") != current:
            raise Conflict(f"key '{k}' is at version {current}", version=current)
        if row:
            tx.execute(
                "UPDATE kv SET value = ?, version = ?, updated_by = ?, updated_at = ? WHERE channel_id = ? AND key = ?",
                (value, current + 1, p.agent_id, now, ch["id"], k),
            )
        else:
            tx.execute(
                "INSERT INTO kv(channel_id, key, value, version, updated_by, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                (ch["id"], k, value, p.agent_id, now),
            )
    _sys(
        db,
        p,
        int(ch["id"]),
        f"@{p.name} set context '{k}' (v{current + 1})",
        {"event": "kv.set", "key": k, "version": current + 1},
        notifier,
    )
    return kv_get(db, p, ch["name"], k)


# -- presence ----------------------------------------------------------------------------

PRESENCE_STATUSES = ("online", "busy", "idle", "offline")


def heartbeat(db: Database, p: Principal, payload: dict[str, Any]) -> dict[str, Any]:
    p.require_writer()
    payload = v.obj(payload)
    status = v.choice(payload.get("status"), "status", PRESENCE_STATUSES, default="online")
    activity = v.text(payload.get("activity"), "activity", 300)
    channel_id = None
    if payload.get("channel"):
        channel_id = int(channel_for(db, p, payload["channel"])["id"])
    db.execute(
        "INSERT INTO presence(agent_id, status, activity, channel_id, last_seen) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (agent_id) DO UPDATE SET status = excluded.status, activity = excluded.activity, "
        "channel_id = excluded.channel_id, last_seen = excluded.last_seen",
        (p.agent_id, status, activity, channel_id, now_ms()),
    )
    return {"agent": p.name, "status": status, "activity": activity}


def presence(db: Database, p: Principal) -> list[dict[str, Any]]:
    now = now_ms()
    rows = db.query(
        "SELECT a.name, a.role, pr.status, pr.activity, pr.last_seen, c.name AS channel FROM presence pr "
        "JOIN agents a ON a.id = pr.agent_id LEFT JOIN channels c ON c.id = pr.channel_id "
        "ORDER BY pr.last_seen DESC"
    )
    return [
        {
            "agent": r["name"],
            "role": r["role"],
            "channel": r["channel"],
            "activity": r["activity"],
            "status": r["status"] if now - int(r["last_seen"]) < ONLINE_WINDOW_MS else "offline",
            "last_seen": iso(int(r["last_seen"])),
        }
        for r in rows
    ]
