"""Messaging: cursors, long-poll, idempotency, threads, mentions, limits, formats."""

from __future__ import annotations

import threading
import time

from .conftest import World


def test_cursor_reads_only_new_messages(world: World) -> None:
    c, alice = world.c, world.h("alice")
    ids = [world.post("alice", f"m{i}").json()["id"] for i in range(3)]
    first = c.get("/api/channels/dev/messages?limit=2", headers=alice).json()
    assert [m["body"] for m in first["messages"]] == ["m1", "m2"]  # latest N, oldest first
    assert first["next_after"] == ids[-1]
    assert c.get(f"/api/channels/dev/messages?after={ids[-1]}", headers=alice).json()["messages"] == []
    world.post("bob", "m3")
    new = c.get(f"/api/channels/dev/messages?after={ids[-1]}", headers=alice).json()
    assert [m["body"] for m in new["messages"]] == ["m3"]
    assert new["messages"][0]["author"] == "bob"


def test_empty_channel_first_read(world: World) -> None:
    r = world.c.get("/api/channels/dev/messages", headers=world.h("alice")).json()
    assert r == {"messages": [], "next_after": 0, "has_more": False}


def test_long_poll_returns_as_soon_as_a_message_arrives(world: World) -> None:
    c = world.c
    head = c.get("/api/channels/dev/messages", headers=world.h("alice")).json()["next_after"]
    timer = threading.Timer(0.5, lambda: world.post("bob", "wake up"))
    timer.start()
    t0 = time.monotonic()
    r = c.get(f"/api/channels/dev/messages?after={head}&wait=5", headers=world.h("alice")).json()
    elapsed = time.monotonic() - t0
    timer.join()
    assert [m["body"] for m in r["messages"]] == ["wake up"]
    assert 0.3 < elapsed < 3


def test_long_poll_times_out_empty(world: World) -> None:
    t0 = time.monotonic()
    r = world.c.get("/api/inbox?after=0&wait=1", headers=world.h("alice")).json()
    assert r["messages"] == [] and time.monotonic() - t0 >= 0.9


def test_idempotent_retries(world: World) -> None:
    a = world.post("alice", "once", client_id="k-1").json()
    b = world.post("alice", "once", client_id="k-1").json()
    assert a["id"] == b["id"] and b["duplicate"] is True
    msgs = world.c.get("/api/channels/dev/messages", headers=world.h("alice")).json()["messages"]
    assert len(msgs) == 1


def test_threads_and_replies(world: World) -> None:
    root = world.post("alice", "question?", kind="question").json()
    r1 = world.post("bob", "answer", kind="answer", reply_to=root["id"]).json()
    r2 = world.post("alice", "follow-up", reply_to=r1["id"]).json()
    assert r1["thread_id"] == root["id"] and r2["thread_id"] == root["id"]
    thread = world.c.get(f"/api/channels/dev/messages?thread={root['id']}", headers=world.h("bob")).json()
    assert [m["id"] for m in thread["messages"]] == [root["id"], r1["id"], r2["id"]]
    world.c.post("/api/channels", json={"name": "other", "members": ["alice"]}, headers=world.admin)
    bad = world.post("alice", "cross", channel="other", reply_to=root["id"])
    assert bad.status_code == 400


def test_mentions_land_in_inbox(world: World) -> None:
    world.post("alice", "@bob please review, cc @nobody", refs=[{"repo": "acme/app", "pr": 7}])
    world.post("alice", "explicit", mentions=["bob"])
    world.post("bob", "self @bob mention is ignored")
    inbox = world.c.get("/api/inbox", headers=world.h("bob")).json()
    assert [m["body"] for m in inbox["messages"]] == ["@bob please review, cc @nobody", "explicit"]
    assert inbox["messages"][0]["mentions"] == ["bob"]
    assert inbox["messages"][0]["refs"] == [{"repo": "acme/app", "pr": 7}]
    after = inbox["next_after"]
    assert world.c.get(f"/api/inbox?after={after}", headers=world.h("bob")).json()["messages"] == []


def test_structured_payloads_and_kind_filter(world: World) -> None:
    world.post("alice", "use pnpm", kind="decision", data={"tool": "pnpm"}, tags=["build"])
    world.post("alice", "chatter")
    r = world.c.get("/api/channels/dev/messages?kinds=decision", headers=world.h("bob")).json()
    assert len(r["messages"]) == 1
    m = r["messages"][0]
    assert m["data"] == {"tool": "pnpm"} and m["tags"] == ["build"] and m["kind"] == "decision"


def test_reserved_kinds_and_validation(world: World) -> None:
    assert world.post("alice", "fake github", kind="event").status_code == 400
    assert world.post("alice", "fake system", kind="system").status_code == 400
    assert world.post("alice", "", kind="chat").status_code == 400
    assert world.post("alice", "x" * 16_001).status_code == 413
    assert world.post("alice", "big", data={"x": "y" * 40_000}).status_code == 413
    assert world.post("alice", "bad refs", refs=[{"evil": 1}]).status_code == 400
    assert world.post("alice", "bad repo", refs=[{"repo": "not a repo"}]).status_code == 400
    r = world.c.post(
        "/api/channels/dev/messages",
        content=b"{not json",
        headers={**world.h("alice"), "content-type": "application/json"},
    )
    assert r.status_code == 400


def test_body_size_cap(world: World) -> None:
    r = world.c.post("/api/channels/dev/messages", content=b"{" + b" " * 300_000 + b"}", headers=world.h("alice"))
    assert r.status_code == 413


def test_text_format_is_compact(world: World) -> None:
    world.post("alice", "ship it @bob", kind="decision", tags=["release"])
    txt = world.c.get("/api/channels/dev/messages?format=text", headers=world.h("bob")).text
    lines = txt.strip().split("\n")
    assert "dev @alice [decision] ->@bob #release: ship it @bob" in lines[0]
    assert lines[-1].startswith("next_after=")


def test_xss_payload_is_stored_verbatim_and_served_as_json(world: World) -> None:
    payload = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    world.post("alice", payload)
    r = world.c.get("/api/channels/dev/messages", headers=world.h("bob"))
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["messages"][0]["body"] == payload


def test_feed_covers_all_visible_channels(world: World) -> None:
    world.c.post("/api/channels", json={"name": "ops", "members": ["alice"]}, headers=world.admin)
    world.post("alice", "in dev")
    world.post("alice", "in ops", channel="ops")
    assert [m["body"] for m in world.c.get("/api/feed", headers=world.h("alice")).json()["messages"]] == [
        "in dev",
        "in ops",
    ]
    assert [m["body"] for m in world.c.get("/api/feed", headers=world.h("bob")).json()["messages"]] == ["in dev"]


def test_members_can_create_channels_and_invite(world: World) -> None:
    c = world.c
    r = c.post("/api/channels", json={"name": "feature-x", "repo": "acme/app"}, headers=world.h("alice"))
    assert r.status_code == 200 and r.json()["members"] == ["alice"]
    assert c.post("/api/channels/feature-x/members", json={"agent": "bob"}, headers=world.h("alice")).status_code == 200
    assert world.post("bob", "joined", channel="feature-x").status_code == 200
    assert c.delete("/api/channels/feature-x/members/alice", headers=world.h("bob")).status_code == 403


def test_regression_member_sees_only_own_channels_everywhere(world: World) -> None:
    """Regression: the visibility subquery once matched the outer column, leaking other channels."""
    c = world.c
    c.post("/api/channels", json={"name": "ops", "members": ["alice"]}, headers=world.admin)
    world.post("alice", "ops secret @bob", channel="ops")
    assert [ch["name"] for ch in c.get("/api/channels", headers=world.h("bob")).json()] == ["dev"]
    assert c.get("/api/inbox", headers=world.h("bob")).json()["messages"] == []
    assert c.get("/api/channels/ops/messages", headers=world.h("bob")).status_code == 403


def test_wait_is_clamped_to_server_ceiling(world: World) -> None:
    t0 = time.monotonic()
    r = world.c.get("/api/inbox?after=0&wait=600", headers=world.h("alice"))  # ceiling is 5 s in tests
    assert r.status_code == 200 and time.monotonic() - t0 < 7
    assert world.c.get("/api/inbox?wait=-1", headers=world.h("alice")).status_code == 400
