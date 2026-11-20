"""Test fixtures. SQLite by default; set AGENTLINK_TEST_PG_URL to run the same suite on Postgres."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from starlette.testclient import TestClient

from agentlink.app import create_app
from agentlink.config import Settings
from agentlink.db import Database
from agentlink.hub import Hub, build_hub, set_hub

logging.getLogger("httpx").setLevel(logging.WARNING)

ADMIN = "al_test-admin-token-0123456789abcdefghijklmnop"
PG_URL = os.environ.get("AGENTLINK_TEST_PG_URL", "")
TABLES = (
    "mentions messages tasks leases kv presence audit ratelimit archives members tokens channels agents schema_version"
)


def _reset_pg(url: str) -> None:
    db = Database(url)
    for t in TABLES.split():
        db.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    db.close()


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    if PG_URL:
        _reset_pg(PG_URL)
    return Settings(
        admin_token=ADMIN,
        database_url=PG_URL,
        sqlite_path=str(tmp_path / "test.db"),
        github_webhook_secret="whsec-test",
        max_wait_seconds=5,
        poll_interval=0.2,
        write_rate_per_min=10_000,
        read_rate_per_min=100_000,
    )


@pytest.fixture
def hub(settings: Settings) -> Iterator[Hub]:
    h = build_hub(settings)
    set_hub(h)
    yield h
    set_hub(None)
    h.db.close()


@pytest.fixture
def client(hub: Hub) -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=True) as c:
        yield c


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class World:
    """A small team: admin, two members (alice, bob), an outsider and a monitor."""

    def __init__(self, client: TestClient) -> None:
        self.c = client
        self.admin = auth(ADMIN)
        self.tokens: dict[str, str] = {}
        for name, role in (("alice", "member"), ("bob", "member"), ("mallory", "member"), ("watch", "monitor")):
            r = client.post("/api/admin/agents", json={"name": name, "role": role}, headers=self.admin)
            assert r.status_code == 200, r.text
            self.tokens[name] = r.json()["token"]
        r = client.post(
            "/api/channels",
            json={"name": "dev", "repo": "acme/app", "members": ["alice", "bob"]},
            headers=self.admin,
        )
        assert r.status_code == 200, r.text

    def h(self, name: str) -> dict[str, str]:
        return auth(self.tokens[name])

    def post(self, who: str, body: str, channel: str = "dev", **extra: Any) -> Any:
        return self.c.post(f"/api/channels/{channel}/messages", json={"body": body, **extra}, headers=self.h(who))


@pytest.fixture
def world(client: TestClient) -> World:
    return World(client)


@pytest.fixture
def strict_settings(settings: Settings) -> Settings:
    return replace(settings, write_rate_per_min=3)
