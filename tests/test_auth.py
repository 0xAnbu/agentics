"""Security controls: authentication, roles, membership, token lifecycle, rate limits."""

from __future__ import annotations

from starlette.testclient import TestClient

from agentlink.app import create_app
from agentlink.hub import build_hub, set_hub

from .conftest import World, auth


def test_health_is_public_but_everything_else_requires_a_token(client: TestClient) -> None:
    assert client.get("/api/health").json()["ok"] is True
    for path in ("/api/me", "/api/channels", "/api/feed", "/api/inbox", "/api/stats", "/api/admin/agents"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert r.headers["www-authenticate"] == "Bearer"


def test_garbage_and_unknown_tokens_are_rejected(client: TestClient) -> None:
    for header in ("Bearer nope", "Basic abc", "Bearer al_doesnotexist", "Bearer " + "al_" + "x" * 300):
        assert client.get("/api/me", headers={"Authorization": header}).status_code == 401


def test_tokens_are_stored_only_as_hashes(world: World, hub) -> None:
    rows = hub.db.query("SELECT token_hash FROM tokens")
    stored = {r["token_hash"] for r in rows}
    for token in world.tokens.values():
        assert token not in stored
        assert all(token not in s for s in stored)


def test_member_cannot_access_channels_it_does_not_belong_to(world: World) -> None:
    c = world.c
    assert c.get("/api/channels/dev/messages", headers=world.h("mallory")).status_code == 403
    assert world.post("mallory", "hi").status_code == 403
    assert c.get("/api/channels", headers=world.h("mallory")).json() == []
    feed = c.get("/api/feed", headers=world.h("mallory")).json()
    world.post("alice", "secret plan")
    assert c.get("/api/feed", headers=world.h("mallory")).json()["messages"] == feed["messages"] == []


def test_monitor_reads_everything_but_cannot_write(world: World) -> None:
    world.post("alice", "hello")
    c = world.c
    r = c.get("/api/channels/dev/messages", headers=world.h("watch"))
    assert r.status_code == 200 and r.json()["messages"][0]["body"] == "hello"
    assert c.get("/api/stats", headers=world.h("watch")).status_code == 200
    assert world.post("watch", "nope").status_code == 403
    assert c.post("/api/presence", json={}, headers=world.h("watch")).status_code == 403


def test_admin_endpoints_require_admin(world: World) -> None:
    c = world.c
    alice = world.h("alice")
    assert c.post("/api/admin/agents", json={"name": "evil"}, headers=alice).status_code == 403
    assert c.get("/api/admin/audit", headers=alice).status_code == 403
    assert c.get("/api/admin/archive/plan?older_than_days=0", headers=alice).status_code == 403
    assert c.get("/api/stats", headers=alice).status_code == 403


def test_revoked_rotated_and_disabled_tokens(world: World) -> None:
    c = world.c
    agents = c.get("/api/admin/agents", headers=world.admin).json()
    bob_token_id = next(a for a in agents if a["name"] == "bob")["tokens"][0]["id"]
    new = c.post("/api/admin/agents/bob/tokens", json={"label": "rotation"}, headers=world.admin).json()["token"]
    assert c.delete(f"/api/admin/tokens/{bob_token_id}", headers=world.admin).status_code == 200
    assert c.get("/api/me", headers=world.h("bob")).status_code == 401
    assert c.get("/api/me", headers=auth(new)).status_code == 200
    assert c.post("/api/admin/agents/bob/disable", headers=world.admin).status_code == 200
    assert c.get("/api/me", headers=auth(new)).status_code == 401
    audit = c.get("/api/admin/audit", headers=world.admin).json()
    assert {"token.revoke", "agent.disable", "token.create"} <= {a["action"] for a in audit}


def test_expired_token_is_rejected(world: World, hub) -> None:
    hub.db.execute("UPDATE tokens SET expires_at = 1 WHERE agent_id = (SELECT id FROM agents WHERE name = 'alice')")
    assert world.c.get("/api/me", headers=world.h("alice")).status_code == 401


def test_write_rate_limit(strict_settings) -> None:
    h = build_hub(strict_settings)
    set_hub(h)
    try:
        with TestClient(create_app()) as c:
            world = World.__new__(World)
            world.c, world.admin, world.tokens = c, auth(strict_settings.admin_token), {}
            codes = [c.post("/api/channels", json={"name": f"c{i}"}, headers=world.admin).status_code for i in range(5)]
            assert codes[:3] == [200, 200, 200]
            assert codes[3] == 429
    finally:
        set_hub(None)
        h.db.close()


def test_duplicate_agent_and_invalid_names(world: World) -> None:
    c = world.c
    assert c.post("/api/admin/agents", json={"name": "alice"}, headers=world.admin).status_code == 409
    for bad in ("Alice", "a b", "../x", "", "x" * 70):
        assert c.post("/api/admin/agents", json={"name": bad}, headers=world.admin).status_code == 400
