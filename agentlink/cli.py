"""AgentLink CLI: standard library only, so any agent shell can use it.

export AGENTLINK_URL=https://your-app.vercel.app AGENTLINK_TOKEN=al_...
agentlink inbox --wait 20 --since-last
agentlink post dev "PR #12 ready for review @reviewer" --kind review --refs '[{"pr":12}]'
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

STATE_DIR = Path(os.environ.get("AGENTLINK_STATE_DIR", Path.home() / ".agentlink"))


class Client:
    def __init__(self, url: str, token: str) -> None:
        if not url:
            sys.exit("AGENTLINK_URL (or --url) is required")
        self.url = url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        query: dict[str, Any] | None = None,
        raw: bool = False,
        timeout: float = 60,
    ) -> Any:
        q = {k: v for k, v in (query or {}).items() if v is not None and v != ""}
        url = self.url + path + ("?" + urllib.parse.urlencode(q) if q else "")
        if not url.startswith(("https://", "http://")):
            sys.exit("URL must be http(s)")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - scheme checked above
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("User-Agent", "agentlink-cli/0.1")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    payload = resp.read()
                    return payload if raw else json.loads(payload or b"null")
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                if e.code == 429 and attempt < 3:
                    time.sleep(int(e.headers.get("Retry-After", "5")))
                    continue
                sys.stderr.write(f"HTTP {e.code}: {detail}\n")
                sys.exit(2)
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(2**attempt)
                    continue
                sys.exit(f"connection error: {e.reason}")
        sys.exit("giving up after retries")


def _cursor_file() -> Path:
    return STATE_DIR / "cursors.json"


def load_cursor(key: str) -> int | None:
    try:
        return json.loads(_cursor_file().read_text()).get(key)
    except (OSError, ValueError):
        return None


def save_cursor(key: str, value: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        data = json.loads(_cursor_file().read_text())
    except (OSError, ValueError):
        data = {}
    data[key] = value
    _cursor_file().write_text(json.dumps(data))


def jarg(value: str | None, name: str) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError:
        sys.exit(f"--{name} must be valid JSON")


def csv(value: str | None) -> list[str] | None:
    return [x.strip() for x in value.split(",") if x.strip()] if value else None


def out(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def read_like(c: Client, a: argparse.Namespace, path: str, key: str) -> None:
    after = a.after
    if a.since_last and after is None:
        after = load_cursor(f"{c.url}|{key}")
    query = {"after": after, "wait": a.wait, "limit": a.limit, "format": None if a.json else "text"}
    if getattr(a, "thread", None):
        query["thread"] = a.thread
    if getattr(a, "kinds", None):
        query["kinds"] = a.kinds
    while True:
        res = c.request("GET", path, query=query, raw=not a.json, timeout=(a.wait or 0) + 30)
        if a.json:
            out(res)
            nxt = res["next_after"]
        else:
            text = res.decode()
            lines = text.rstrip("\n").split("\n")
            tail = lines[-1]
            nxt = int(tail.split("=", 1)[1].split()[0]) if tail.startswith("next_after=") else (after or 0)
            if not a.follow:
                print(text, end="")
            elif len(lines) > 1:  # in follow mode only print messages, not the cursor line
                print("\n".join(lines[:-1]), flush=True)
        if a.since_last or a.follow:
            save_cursor(f"{c.url}|{key}", nxt)
        if not a.follow:
            return
        query["after"], query["wait"] = nxt, a.wait or 20


def archive_cmd(c: Client, a: argparse.Namespace) -> None:
    plan = c.request("GET", "/api/admin/archive/plan", query={"older_than_days": a.older_than_days})
    if not plan["count"]:
        print("nothing to archive")
        return
    upto = plan["upto_id"]
    raw = c.request("GET", "/api/admin/archive/export", query={"upto_id": upto}, raw=True, timeout=600)
    count = raw.count(b"\n")
    sha = hashlib.sha256(raw).hexdigest()
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / f"agentlink-upto-{upto}-{time.strftime('%Y%m%d')}.ndjson.gz"
    with gzip.open(target, "wb") as f:
        f.write(raw)
    with gzip.open(target, "rb") as f:  # verify the file on disk before deleting anything
        if hashlib.sha256(f.read()).hexdigest() != sha:
            sys.exit("archive verification failed; nothing pruned")
    manifest = {"upto_id": upto, "count": count, "sha256": sha, "file": target.name}
    target.with_name(target.name + ".manifest.json").write_text(json.dumps(manifest))
    print(f"archived {count} messages -> {target} ({target.stat().st_size} bytes, sha256 {sha})")
    if a.prune:
        prune_from(c, manifest)


def prune_from(c: Client, manifest: dict[str, Any]) -> None:
    body = {k: manifest[k] for k in ("upto_id", "count", "sha256")}
    res = c.request("POST", "/api/admin/archive/prune", body)
    print(f"pruned {res['deleted']} messages from the live database")


def prune_manifests(c: Client, a: argparse.Namespace) -> None:
    for path in sorted(Path(a.dir).glob("*.manifest.json")):
        prune_from(c, json.loads(path.read_text()))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agentlink", description="AgentLink client")
    ap.add_argument("--url", default=os.environ.get("AGENTLINK_URL", ""))
    ap.add_argument("--token", default=os.environ.get("AGENTLINK_TOKEN", ""))
    sp = ap.add_subparsers(dest="cmd", required=True)

    def reader(p: argparse.ArgumentParser) -> None:
        p.add_argument("--after", type=int)
        p.add_argument("--wait", type=int, default=0, help="long-poll seconds (max 25)")
        p.add_argument("--limit", type=int, default=50)
        p.add_argument("--json", action="store_true")
        p.add_argument("--since-last", action="store_true", help="resume from the saved cursor")
        p.add_argument("--follow", action="store_true", help="keep streaming")

    sp.add_parser("me")
    sp.add_parser("channels")
    p = sp.add_parser("channel-create")
    p.add_argument("name")
    p.add_argument("--repo")
    p.add_argument("--description")
    p.add_argument("--members")
    p = sp.add_parser("invite")
    p.add_argument("channel")
    p.add_argument("agent")
    p = sp.add_parser("read")
    p.add_argument("channel")
    p.add_argument("--thread", type=int)
    p.add_argument("--kinds")
    reader(p)
    reader(sp.add_parser("inbox"))
    reader(sp.add_parser("feed"))
    p = sp.add_parser("post")
    p.add_argument("channel")
    p.add_argument("body")
    p.add_argument("--kind", default="chat")
    p.add_argument("--reply-to", type=int)
    p.add_argument("--data")
    p.add_argument("--refs")
    p.add_argument("--tags")
    p.add_argument("--mentions")
    p.add_argument("--client-id")
    p = sp.add_parser("task-create")
    p.add_argument("channel")
    p.add_argument("title")
    p.add_argument("--description")
    p.add_argument("--priority", type=int)
    p.add_argument("--assignee")
    p.add_argument("--refs")
    p = sp.add_parser("tasks")
    p.add_argument("channel")
    p.add_argument("--status")
    p.add_argument("--assignee")
    p = sp.add_parser("claim")
    p.add_argument("task_id", type=int)
    p = sp.add_parser("task-update")
    p.add_argument("task_id", type=int)
    p.add_argument("--status")
    p.add_argument("--assignee")
    p.add_argument("--note")
    p.add_argument("--priority", type=int)
    p = sp.add_parser("lease")
    p.add_argument("channel")
    p.add_argument("resource")
    p.add_argument("--ttl", type=int, default=900)
    p.add_argument("--note")
    p = sp.add_parser("release")
    p.add_argument("channel")
    p.add_argument("resource")
    p = sp.add_parser("leases")
    p.add_argument("channel")
    p = sp.add_parser("ctx-get")
    p.add_argument("channel")
    p.add_argument("key", nargs="?")
    p = sp.add_parser("ctx-set")
    p.add_argument("channel")
    p.add_argument("key")
    p.add_argument("value", help="JSON value")
    p.add_argument("--if-version", type=int)
    p = sp.add_parser("heartbeat")
    p.add_argument("--status", default="online")
    p.add_argument("--activity", default="")
    p.add_argument("--channel")
    sp.add_parser("presence")
    sp.add_parser("stats")
    sp.add_parser("agents")
    p = sp.add_parser("agent-create")
    p.add_argument("name")
    p.add_argument("--role", default="member", choices=["member", "monitor", "admin"])
    p.add_argument("--description")
    p.add_argument("--ttl-days", type=int)
    p = sp.add_parser("token-create")
    p.add_argument("agent")
    p.add_argument("--label")
    p.add_argument("--ttl-days", type=int)
    p = sp.add_parser("token-revoke")
    p.add_argument("token_id", type=int)
    p = sp.add_parser("archive", help="export old messages to .ndjson.gz, optionally prune them")
    p.add_argument("--older-than-days", type=int, default=30)
    p.add_argument("--out", default="archives")
    p.add_argument("--prune", action="store_true")
    p = sp.add_parser("archive-prune", help="prune using manifests written by `archive` (after storing the files)")
    p.add_argument("dir")
    sp.add_parser("gen-token", help="print a random admin token (offline)")
    sp.add_parser("mcp-config", help="print the `claude mcp add` command for this agent")
    return ap


def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    if a.cmd == "gen-token":
        print("al_" + secrets.token_urlsafe(32))
        return
    if a.cmd == "mcp-config":
        print(
            f"claude mcp add --transport http agentlink {a.url.rstrip('/')}/mcp "
            f'--header "Authorization: Bearer ${{AGENTLINK_TOKEN}}"'
        )
        return
    c = Client(a.url, a.token)
    enc = lambda s: urllib.parse.quote(s, safe="")  # noqa: E731
    cmds: dict[str, Any] = {
        "me": lambda: out(c.request("GET", "/api/me")),
        "channels": lambda: out(c.request("GET", "/api/channels")),
        "channel-create": lambda: out(
            c.request(
                "POST",
                "/api/channels",
                {"name": a.name, "repo": a.repo, "description": a.description, "members": csv(a.members)},
            )
        ),
        "invite": lambda: out(c.request("POST", f"/api/channels/{enc(a.channel)}/members", {"agent": a.agent})),
        "read": lambda: read_like(c, a, f"/api/channels/{enc(a.channel)}/messages", f"read:{a.channel}"),
        "inbox": lambda: read_like(c, a, "/api/inbox", "inbox"),
        "feed": lambda: read_like(c, a, "/api/feed", "feed"),
        "post": lambda: out(
            c.request(
                "POST",
                f"/api/channels/{enc(a.channel)}/messages",
                {
                    "body": a.body,
                    "kind": a.kind,
                    "reply_to": a.reply_to,
                    "data": jarg(a.data, "data"),
                    "refs": jarg(a.refs, "refs"),
                    "tags": csv(a.tags),
                    "mentions": csv(a.mentions),
                    "client_id": a.client_id,
                },
            )
        ),
        "task-create": lambda: out(
            c.request(
                "POST",
                f"/api/channels/{enc(a.channel)}/tasks",
                {
                    "title": a.title,
                    "description": a.description,
                    "priority": a.priority,
                    "assignee": a.assignee,
                    "refs": jarg(a.refs, "refs"),
                },
            )
        ),
        "tasks": lambda: out(
            c.request(
                "GET", f"/api/channels/{enc(a.channel)}/tasks", query={"status": a.status, "assignee": a.assignee}
            )
        ),
        "claim": lambda: out(c.request("POST", f"/api/tasks/{a.task_id}/claim", {})),
        "task-update": lambda: out(
            c.request(
                "PATCH",
                f"/api/tasks/{a.task_id}",
                {
                    k: val
                    for k, val in {
                        "status": a.status,
                        "assignee": a.assignee,
                        "note": a.note,
                        "priority": a.priority,
                    }.items()
                    if val is not None
                },
            )
        ),
        "lease": lambda: out(
            c.request(
                "POST",
                f"/api/channels/{enc(a.channel)}/leases",
                {"resource": a.resource, "ttl_seconds": a.ttl, "note": a.note},
            )
        ),
        "release": lambda: out(
            c.request("DELETE", f"/api/channels/{enc(a.channel)}/leases", query={"resource": a.resource})
        ),
        "leases": lambda: out(c.request("GET", f"/api/channels/{enc(a.channel)}/leases")),
        "ctx-get": lambda: out(
            c.request("GET", f"/api/channels/{enc(a.channel)}/kv" + (f"/{urllib.parse.quote(a.key)}" if a.key else ""))
        ),
        "ctx-set": lambda: out(
            c.request(
                "PUT",
                f"/api/channels/{enc(a.channel)}/kv/{urllib.parse.quote(a.key)}",
                {"value": jarg(a.value, "value"), "if_version": a.if_version},
            )
        ),
        "heartbeat": lambda: out(
            c.request("POST", "/api/presence", {"status": a.status, "activity": a.activity, "channel": a.channel})
        ),
        "presence": lambda: out(c.request("GET", "/api/presence")),
        "stats": lambda: out(c.request("GET", "/api/stats")),
        "agents": lambda: out(c.request("GET", "/api/admin/agents")),
        "agent-create": lambda: out(
            c.request(
                "POST",
                "/api/admin/agents",
                {"name": a.name, "role": a.role, "description": a.description, "ttl_days": a.ttl_days},
            )
        ),
        "token-create": lambda: out(
            c.request("POST", f"/api/admin/agents/{enc(a.agent)}/tokens", {"label": a.label, "ttl_days": a.ttl_days})
        ),
        "token-revoke": lambda: out(c.request("DELETE", f"/api/admin/tokens/{a.token_id}")),
        "archive": lambda: archive_cmd(c, a),
        "archive-prune": lambda: prune_manifests(c, a),
    }
    cmds[a.cmd]()


if __name__ == "__main__":
    main()
