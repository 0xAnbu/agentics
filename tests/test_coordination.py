"""Tasks (atomic claim), leases (overlap/TTL), shared context (versions), presence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from agentlink.coordination import overlaps
from agentlink.hub import Hub
from agentlink.security import now_ms

from .conftest import World, auth


def _task(world: World, **kw) -> dict:
    r = world.c.post("/api/channels/dev/tasks", json={"title": "Add login", **kw}, headers=world.h("alice"))
    assert r.status_code == 200, r.text
    return r.json()


def test_concurrent_claim_has_exactly_one_winner(world: World, hub: Hub) -> None:
    task = _task(world)
    names = []
    for i in range(8):
        r = world.c.post("/api/admin/agents", json={"name": f"agent{i}"}, headers=world.admin).json()
        world.c.post("/api/channels/dev/members", json={"agent": f"agent{i}"}, headers=world.admin)
        names.append(r["token"])

    def claim(token: str) -> int:
        return world.c.post(f"/api/tasks/{task['id']}/claim", headers=auth(token)).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(claim, names))
    assert codes.count(200) == 1 and codes.count(409) == 7
    final = world.c.get(f"/api/tasks/{task['id']}", headers=world.h("alice")).json()
    assert final["status"] == "claimed" and final["assignee"].startswith("agent")


def test_task_lifecycle_and_permissions(world: World) -> None:
    c = world.c
    task = _task(world, priority=1, refs=[{"repo": "acme/app", "issue": 3}])
    assert c.post(f"/api/tasks/{task['id']}/claim", headers=world.h("bob")).json()["assignee"] == "bob"
    # idempotent for the owner
    assert c.post(f"/api/tasks/{task['id']}/claim", headers=world.h("bob")).status_code == 200
    r = c.patch(f"/api/tasks/{task['id']}", json={"status": "in_progress"}, headers=world.h("bob"))
    assert r.json()["status"] == "in_progress"
    # outsiders and non-owners cannot update
    world.c.post("/api/channels/dev/members", json={"agent": "mallory"}, headers=world.admin)
    assert c.patch(f"/api/tasks/{task['id']}", json={"status": "done"}, headers=world.h("mallory")).status_code == 403
    # optimistic concurrency
    stale = c.patch(f"/api/tasks/{task['id']}", json={"status": "review", "if_version": 1}, headers=world.h("bob"))
    assert stale.status_code == 409
    done = c.patch(
        f"/api/tasks/{task['id']}", json={"status": "done", "note": "merged in #9"}, headers=world.h("bob")
    ).json()
    assert done["status"] == "done"
    assert c.get("/api/channels/dev/tasks", headers=world.h("alice")).json() == []
    assert len(c.get("/api/channels/dev/tasks?all=1", headers=world.h("alice")).json()) == 1
    log = c.get("/api/channels/dev/messages?kinds=task,system", headers=world.h("alice")).json()["messages"]
    assert any("claimed task" in m["body"] for m in log) and any("merged in #9" in m["body"] for m in log)
    # creator was mentioned on updates by others
    inbox = c.get("/api/inbox", headers=world.h("alice")).json()
    assert any(m["data"].get("task_id") == task["id"] for m in inbox["messages"])


def test_assigning_a_task_puts_it_in_the_assignee_inbox(world: World) -> None:
    task = _task(world, assignee="bob")
    inbox = world.c.get("/api/inbox", headers=world.h("bob")).json()
    assert inbox["open_tasks"][0]["id"] == task["id"]
    assert inbox["messages"][0]["kind"] == "task"


def test_overlap_rules() -> None:
    assert overlaps("src/auth", "src/auth/login.py")
    assert overlaps("src/auth/**", "src/auth/x/y.py")
    assert overlaps("src/a.py", "src/a.py")
    assert not overlaps("src/auth", "src/authz/x.py")
    assert not overlaps("docs/a.md", "src/a.md")


def test_lease_conflict_renew_release(world: World) -> None:
    c = world.c
    r = c.post("/api/channels/dev/leases", json={"resource": "./src/auth/", "note": "login"}, headers=world.h("alice"))
    assert r.status_code == 200 and r.json()["resource"] == "src/auth/" and r.json()["renewed"] is False
    clash = c.post("/api/channels/dev/leases", json={"resource": "src/auth/jwt.py"}, headers=world.h("bob"))
    assert clash.status_code == 409 and clash.json()["lease"]["holder"] == "alice"
    assert c.post("/api/channels/dev/leases", json={"resource": "src/ui"}, headers=world.h("bob")).status_code == 200
    renew = c.post("/api/channels/dev/leases", json={"resource": "src/auth/"}, headers=world.h("alice"))
    assert renew.json()["renewed"] is True
    assert len(c.get("/api/channels/dev/leases", headers=world.h("bob")).json()) == 2
    assert c.delete("/api/channels/dev/leases?resource=src/auth/", headers=world.h("bob")).status_code == 403
    assert c.delete("/api/channels/dev/leases?resource=src/auth/", headers=world.h("alice")).status_code == 200
    assert (
        c.post("/api/channels/dev/leases", json={"resource": "src/auth/jwt.py"}, headers=world.h("bob")).status_code
        == 200
    )
    for bad in ("../etc/passwd", "", "a/../b"):
        assert c.post("/api/channels/dev/leases", json={"resource": bad}, headers=world.h("bob")).status_code == 400


def test_expired_leases_are_ignored(world: World, hub: Hub) -> None:
    c = world.c
    c.post("/api/channels/dev/leases", json={"resource": "src/x.py", "ttl_seconds": 10}, headers=world.h("alice"))
    hub.db.execute("UPDATE leases SET expires_at = ?", (now_ms() - 1,))
    assert c.get("/api/channels/dev/leases", headers=world.h("bob")).json() == []
    assert c.post("/api/channels/dev/leases", json={"resource": "src/x.py"}, headers=world.h("bob")).status_code == 200


def test_shared_context_versions(world: World) -> None:
    c = world.c
    url = "/api/channels/dev/kv/conventions/style"
    v1 = c.put(url, json={"value": {"formatter": "ruff"}}, headers=world.h("alice")).json()
    assert v1["version"] == 1 and v1["value"] == {"formatter": "ruff"}
    stale = c.put(url, json={"value": {"formatter": "black"}, "if_version": 0}, headers=world.h("bob"))
    assert stale.status_code == 409 and stale.json()["version"] == 1
    v2 = c.put(url, json={"value": {"formatter": "black"}, "if_version": 1}, headers=world.h("bob")).json()
    assert v2["version"] == 2 and v2["updated_by"] == "bob"
    assert c.get(url, headers=world.h("alice")).json()["value"] == {"formatter": "black"}
    assert [k["key"] for k in c.get("/api/channels/dev/kv?prefix=conv", headers=world.h("alice")).json()] == [
        "conventions/style"
    ]
    assert c.get("/api/channels/dev/kv/missing", headers=world.h("alice")).status_code == 404
    assert c.put(url, json={}, headers=world.h("alice")).status_code == 400


def test_presence(world: World) -> None:
    c = world.c
    c.post(
        "/api/presence", json={"status": "busy", "activity": "writing tests", "channel": "dev"}, headers=world.h("bob")
    )
    p = c.get("/api/presence", headers=world.h("alice")).json()
    assert p[0]["agent"] == "bob" and p[0]["status"] == "busy" and p[0]["channel"] == "dev"
    assert c.post("/api/presence", json={"status": "sleeping"}, headers=world.h("bob")).status_code == 400
