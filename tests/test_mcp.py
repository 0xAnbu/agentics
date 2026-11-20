"""MCP Streamable HTTP endpoint."""

from __future__ import annotations

import json

from .conftest import World


def rpc(world: World, who: str, method: str, params: dict | None = None, id_: int | None = 1, **headers):
    body: dict = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        body["id"] = id_
    if params is not None:
        body["params"] = params
    return world.c.post("/mcp", json=body, headers={**world.h(who), **headers})


def call(world: World, who: str, name: str, **args) -> tuple[bool, str]:
    r = rpc(world, who, "tools/call", {"name": name, "arguments": args})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    return res["isError"], res["content"][0]["text"]


def test_handshake_and_tool_list(world: World) -> None:
    r = rpc(world, "alice", "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}})
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18" and res["serverInfo"]["name"] == "agentlink"
    assert "tools" in res["capabilities"] and res["instructions"]
    assert rpc(world, "alice", "notifications/initialized", id_=None).status_code == 202
    tools = {t["name"] for t in rpc(world, "alice", "tools/list").json()["result"]["tools"]}
    assert {"post_message", "read_messages", "inbox", "claim_task", "acquire_lease", "set_context"} <= tools
    assert rpc(world, "alice", "ping").json()["result"] == {}
    assert rpc(world, "alice", "nope/method").json()["error"]["code"] == -32601


def test_tools_end_to_end(world: World) -> None:
    err, text = call(world, "alice", "post_message", channel="dev", body="hi @bob", kind="question")
    assert not err and json.loads(text)["mentions"] == ["bob"]
    err, text = call(world, "bob", "inbox")
    assert not err and "@alice [question]" in text and "next_after=" in text
    err, text = call(world, "bob", "read_messages", channel="dev", format="json")
    assert json.loads(text)["messages"][0]["body"] == "hi @bob"
    err, text = call(world, "alice", "create_task", channel="dev", title="Write docs")
    tid = json.loads(text)["id"]
    assert not call(world, "bob", "claim_task", task_id=tid)[0]
    err, text = call(world, "alice", "claim_task", task_id=tid)
    assert err and "conflict" in text
    assert not call(world, "bob", "acquire_lease", channel="dev", resource="docs/")[0]
    err, text = call(world, "alice", "acquire_lease", channel="dev", resource="docs/README.md")
    assert err and "bob" in text
    assert not call(world, "alice", "set_context", channel="dev", key="decisions/db", value="postgres")[0]
    assert json.loads(call(world, "bob", "get_context", channel="dev", key="decisions/db")[1])["value"] == "postgres"
    assert not call(world, "bob", "heartbeat", status="busy", activity="docs")[0]
    assert "docs" in call(world, "alice", "presence")[1]


def test_tool_errors_are_reported_not_raised(world: World) -> None:
    err, text = call(world, "mallory", "read_messages", channel="dev")
    assert err and "forbidden" in text
    err, text = call(world, "watch", "post_message", channel="dev", body="x")
    assert err and "read-only" in text
    r = rpc(world, "alice", "tools/call", {"name": "rm_rf", "arguments": {}})
    assert r.json()["error"]["code"] == -32602


def test_transport_rules(world: World) -> None:
    c = world.c
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert c.get("/mcp", headers=world.h("alice")).status_code == 405
    evil = rpc(world, "alice", "ping", origin="https://evil.example")
    assert evil.status_code == 403
    batch = c.post(
        "/mcp",
        json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/x"}],
        headers=world.h("alice"),
    )
    assert batch.status_code == 200 and len(batch.json()) == 1
    assert c.post("/mcp", json={"foo": 1}, headers=world.h("alice")).json()["error"]["code"] == -32600
