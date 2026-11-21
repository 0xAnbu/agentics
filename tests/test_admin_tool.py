"""The human admin tool (agentlink.admin) driven against the real app."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from agentlink import admin

from .conftest import ADMIN, World, auth


class Bridge:
    """Routes the tool's requests to the in-process app instead of the network."""

    url = "http://testserver"

    def __init__(self, client: TestClient, token: str = ADMIN) -> None:
        self.c, self.token = client, token

    def request(self, method: str, path: str, body: Any = None, query: dict[str, Any] | None = None) -> Any:
        r = self.c.request(method, path, json=body, params=query, headers=auth(self.token))
        assert r.status_code == 200, r.text
        return r.json()


def run(world: World, capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    admin.main(list(argv), client=Bridge(world.c))
    return capsys.readouterr().out


def test_users_lists_agents_as_a_table(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    out = run(world, capsys, "users", "list")
    assert out.splitlines()[0].startswith("NOMBRE")
    assert "alice" in out and "watch" in out and "monitor" in out


def test_add_rotate_and_revoke(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    out = run(world, capsys, "users", "add", "carol", "--monitor")
    first = next(w for w in out.split() if w.startswith("al_"))
    assert world.c.get("/api/me", headers=auth(first)).json()["role"] == "monitor"

    out = run(world, capsys, "tokens", "add", "carol", "--replace")
    second = next(w for w in out.split() if w.startswith("al_"))
    assert world.c.get("/api/me", headers=auth(first)).status_code == 401
    assert world.c.get("/api/me", headers=auth(second)).status_code == 200

    tokens = run(world, capsys, "tokens", "list", "carol")
    assert "revocado" in tokens and "activo" in tokens
    active_id = next(line.split()[0] for line in tokens.splitlines() if " activo " in line)
    run(world, capsys, "tokens", "remove", active_id, "--yes")
    assert world.c.get("/api/me", headers=auth(second)).status_code == 401


def test_disable_enable_and_membership(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    run(world, capsys, "users", "disable", "alice", "-y")
    assert world.c.get("/api/me", headers=world.h("alice")).status_code in (401, 403)
    assert "BLOQUEADO" in run(world, capsys, "users", "list")
    run(world, capsys, "users", "enable", "alice")
    assert world.c.get("/api/me", headers=world.h("alice")).status_code == 200

    run(world, capsys, "channels", "kick", "dev", "bob")
    assert "bob" not in run(world, capsys, "channels", "list")
    assert "bob" in run(world, capsys, "channels", "join", "dev", "bob")


def test_local_env_token_is_picked_up(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("DATABASE_URL=\nAGENTLINK_ADMIN_TOKEN=al_local\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTLINK_TOKEN", raising=False)
    monkeypatch.setattr(admin, "CONFIG_FILE", tmp_path / "none.json")
    assert admin.resolve("", "")[:2] == (admin.LOCAL_URL, "al_local")


def test_admin_deletes_message_and_channel(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    keep = world.post("alice", "keep me").json()["id"]
    gone = world.post("alice", "hey @bob delete me").json()["id"]
    run(world, capsys, "messages", "remove", str(gone), "-y")
    ids = [m["id"] for m in world.c.get("/api/channels/dev/messages", headers=world.h("bob")).json()["messages"]]
    assert gone not in ids and keep in ids
    assert world.c.get("/api/inbox", headers=world.h("bob")).json()["messages"] == []

    world.c.post("/api/channels/dev/tasks", json={"title": "t"}, headers=world.h("alice"))
    out = run(world, capsys, "channels", "remove", "dev", "-y")
    assert "dev" in out
    assert world.c.get("/api/channels/dev", headers=world.admin).status_code == 404
    assert "message.delete" in run(world, capsys, "log") and "channel.delete" in run(world, capsys, "log")


def test_only_admin_can_delete(world: World) -> None:
    mid = world.post("alice", "mine").json()["id"]
    for who in ("alice", "watch"):
        assert world.c.delete(f"/api/admin/messages/{mid}", headers=world.h(who)).status_code == 403
        assert world.c.delete("/api/admin/channels/dev", headers=world.h(who)).status_code == 403
    assert world.c.delete("/api/admin/messages/999999", headers=world.admin).status_code == 404


def test_add_with_explicit_member_role(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    token = next(w for w in run(world, capsys, "users", "add", "dave", "--member").split() if w.startswith("al_"))
    assert world.c.get("/api/me", headers=auth(token)).json()["role"] == "member"


def test_delete_agent_keeps_history_and_frees_name(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    mid = world.post("alice", "from alice").json()["id"]
    world.c.post("/api/channels/dev/tasks", json={"title": "t", "assignee": "alice"}, headers=world.h("bob"))
    world.c.post("/api/channels/dev/leases", json={"resource": "src/"}, headers=world.h("alice"))
    out = run(world, capsys, "users", "remove", "alice", "-y")
    assert "alice" in out
    assert world.c.get("/api/me", headers=world.h("alice")).status_code == 401
    assert "alice" not in run(world, capsys, "users", "list")
    msgs = world.c.get("/api/channels/dev/messages", headers=world.h("bob")).json()["messages"]
    assert next(m for m in msgs if m["id"] == mid)["author"].startswith("alice (deleted #")
    tasks = world.c.get("/api/channels/dev/tasks", headers=world.h("bob")).json()
    assert tasks[0]["status"] == "open" and not tasks[0].get("assignee")
    assert world.c.get("/api/channels/dev/leases", headers=world.h("bob")).json() == []
    assert "alice" not in world.c.get("/api/channels/dev", headers=world.admin).json()["members"]
    run(world, capsys, "users", "add", "alice")  # the name is free again


def test_delete_agent_with_messages_and_guards(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    mid = world.post("alice", "bye").json()["id"]
    run(world, capsys, "users", "remove", "alice", "--with-messages", "-y")
    ids = [m["id"] for m in world.c.get("/api/channels/dev/messages", headers=world.h("bob")).json()["messages"]]
    assert mid not in ids
    assert world.c.delete("/api/admin/agents/bob", headers=world.h("mallory")).status_code == 403
    assert world.c.delete("/api/admin/agents/admin", headers=world.admin).status_code == 400


def test_channel_add_and_group_defaults(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    out = run(world, capsys, "channels", "add", "ops", "--repo", "acme/ops", "--members", "alice,bob")
    assert "ops" in out and "alice" in out
    listing = run(world, capsys, "channels")  # group with no action lists
    assert "ops" in listing and "acme/ops" in listing
    assert run(world, capsys, "users").splitlines()[0].startswith("NOMBRE")
    assert "ACCIÓN" in run(world, capsys, "tokens")  # no default action: shows the group's help
