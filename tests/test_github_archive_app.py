"""GitHub webhook, verified archive/prune, stats, static UI and security headers."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from .conftest import World

STATIC = Path(__file__).resolve().parent.parent / "agentlink" / "static"


def _hook(world: World, event: str, payload: dict, secret: str = "whsec-test", delivery: str = "d-1"):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return world.c.post(
        "/api/github/webhook",
        content=raw,
        headers={"x-github-event": event, "x-hub-signature-256": sig, "x-github-delivery": delivery},
    )


PUSH = {
    "ref": "refs/heads/feat/login",
    "after": "0123456789abcdef",
    "commits": [{}, {}],
    "head_commit": {"message": "Add login form\n\nbody"},
    "repository": {"full_name": "acme/app"},
    "sender": {"login": "octo"},
}


def test_webhook_rejects_bad_signatures(world: World) -> None:
    assert _hook(world, "push", PUSH, secret="wrong").status_code == 401
    r = world.c.post("/api/github/webhook", content=b"{}", headers={"x-github-event": "push"})
    assert r.status_code == 401


def test_webhook_posts_event_once_per_delivery(world: World) -> None:
    assert _hook(world, "push", PUSH).json()["posted"][0]["channel"] == "dev"
    _hook(world, "push", PUSH)  # GitHub redelivery: same delivery id
    msgs = world.c.get("/api/channels/dev/messages", headers=world.h("alice")).json()["messages"]
    assert len(msgs) == 1
    m = msgs[0]
    assert m["kind"] == "event" and m["author"] == "github"
    assert "push feat/login by octo: 2 commit(s)" in m["body"]
    assert m["refs"][0]["branch"] == "feat/login"
    pr = {
        "action": "closed",
        "pull_request": {"number": 9, "title": "Login", "merged": True, "head": {"ref": "feat/login"}},
        "repository": {"full_name": "ACME/app"},
        "sender": {"login": "octo"},
    }
    _hook(world, "pull_request", pr, delivery="d-2")
    last = world.c.get("/api/channels/dev/messages?limit=1", headers=world.h("alice")).json()["messages"][0]
    assert last["body"].startswith("PR #9 merged")
    assert _hook(world, "star", {"repository": {"full_name": "acme/app"}}, delivery="d-3").json() == {"ignored": "star"}


def test_archive_requires_matching_digest(world: World) -> None:
    for i in range(5):
        world.post("alice", f"msg {i} @bob")
    c, admin = world.c, world.admin
    plan = c.get("/api/admin/archive/plan?older_than_days=0", headers=admin).json()
    assert plan["count"] == 5
    raw = c.get(f"/api/admin/archive/export?upto_id={plan['upto_id']}", headers=admin).content
    lines = raw.decode().strip().split("\n")
    assert len(lines) == 5 and json.loads(lines[0])["body"] == "msg 0 @bob"
    bad = c.post(
        "/api/admin/archive/prune", json={"upto_id": plan["upto_id"], "count": 5, "sha256": "0" * 64}, headers=admin
    )
    assert bad.status_code == 409
    assert len(c.get("/api/channels/dev/messages", headers=world.h("alice")).json()["messages"]) == 5
    ok = c.post(
        "/api/admin/archive/prune",
        json={"upto_id": plan["upto_id"], "count": 5, "sha256": hashlib.sha256(raw).hexdigest()},
        headers=admin,
    )
    assert ok.status_code == 200 and ok.json()["deleted"] == 5
    assert c.get("/api/channels/dev/messages", headers=world.h("alice")).json()["messages"] == []
    stats = c.get("/api/stats", headers=admin).json()
    assert stats["archives"][0]["count"] == 5
    # cursors keep working after a prune: ids are never reused
    new = world.post("alice", "after prune").json()
    assert new["id"] > plan["upto_id"]


def test_stats(world: World) -> None:
    world.post("alice", "x", kind="status")
    s = world.c.get("/api/stats", headers=world.h("watch")).json()
    assert s["messages_24h"] == 1 and s["by_agent_24h"][0]["agent"] == "alice" and s["db_bytes"] > 0


def test_ui_is_served_with_strict_headers(client) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "AgentLink" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/health").headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in client.get("/api/health").headers


def test_ui_never_uses_innerhtml() -> None:
    js = (STATIC / "app.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in js
    assert "localStorage" not in js  # token stays per-tab


def test_agent_guide_is_public_markdown(client) -> None:
    r = client.get("/api/agent-guide")
    assert r.status_code == 200 and "acquire_lease" in r.text
