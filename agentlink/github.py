"""GitHub webhook ingestion: verify the HMAC signature, summarise the event and
post it as a trusted ``event`` message into every channel linked to the repo."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from .db import Database
from .directory import channels_for_repo
from .errors import Unauthorized, Unavailable
from .messages import Notifier, post_message
from .security import Principal, now_ms

GITHUB_AGENT = "github"


def verify_signature(secret: str, body: bytes, header: str | None) -> None:
    if not secret:
        raise Unavailable("GITHUB_WEBHOOK_SECRET is not configured")
    if not header or not header.startswith("sha256="):
        raise Unauthorized("missing signature")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header):
        raise Unauthorized("bad signature")


def _github_principal(db: Database) -> Principal:
    row = db.one("SELECT id FROM agents WHERE name = ?", (GITHUB_AGENT,))
    if row is None:
        db.execute(
            "INSERT INTO agents(name, role, description, created_at) VALUES (?, 'member', ?, ?) "
            "ON CONFLICT (name) DO NOTHING",
            (GITHUB_AGENT, "GitHub webhook relay (no tokens)", now_ms()),
        )
        row = db.one("SELECT id FROM agents WHERE name = ?", (GITHUB_AGENT,))
    assert row is not None
    # Admin role *inside the process only*: lets the relay post to any linked channel.
    return Principal(int(row["id"]), GITHUB_AGENT, "admin", None)


def summarize(event: str, p: dict[str, Any]) -> tuple[str, dict[str, Any], list[dict[str, Any]]] | None:
    repo = (p.get("repository") or {}).get("full_name", "")
    sender = (p.get("sender") or {}).get("login", "?")
    refs: list[dict[str, Any]] = [{"repo": repo}] if repo else []
    if event == "push":
        branch = str(p.get("ref", "")).removeprefix("refs/heads/")
        commits = p.get("commits") or []
        head = (p.get("head_commit") or {}).get("message", "").split("\n")[0][:200]
        after = str(p.get("after", ""))[:12]
        refs = [{"repo": repo, "branch": branch, "commit": after}] if repo else []
        return (
            f'push {branch} by {sender}: {len(commits)} commit(s), head {after} "{head}"',
            {"event": "push", "branch": branch, "commits": len(commits), "forced": bool(p.get("forced"))},
            refs,
        )
    if event == "pull_request":
        pr = p.get("pull_request") or {}
        action = p.get("action", "")
        if action == "closed" and pr.get("merged"):
            action = "merged"
        refs = [{"repo": repo, "pr": pr.get("number", 0), "branch": (pr.get("head") or {}).get("ref", "")}]
        return (
            f"PR #{pr.get('number')} {action} by {sender}: {pr.get('title', '')[:200]}",
            {"event": "pull_request", "action": action, "url": pr.get("html_url")},
            refs,
        )
    if event == "issues":
        issue = p.get("issue") or {}
        return (
            f"issue #{issue.get('number')} {p.get('action')} by {sender}: {issue.get('title', '')[:200]}",
            {"event": "issues", "action": p.get("action"), "url": issue.get("html_url")},
            [{"repo": repo, "issue": issue.get("number", 0)}],
        )
    if event == "issue_comment":
        issue = p.get("issue") or {}
        body = (p.get("comment") or {}).get("body", "")[:500]
        return (
            f"comment on #{issue.get('number')} by {sender}: {body}",
            {"event": "issue_comment", "url": (p.get("comment") or {}).get("html_url")},
            [{"repo": repo, "issue": issue.get("number", 0)}],
        )
    if event == "workflow_run":
        run = p.get("workflow_run") or {}
        if p.get("action") != "completed":
            return None
        return (
            f"CI {run.get('name')} {run.get('conclusion')} on {run.get('head_branch')} "
            f"({str(run.get('head_sha', ''))[:12]})",
            {"event": "workflow_run", "conclusion": run.get("conclusion"), "url": run.get("html_url")},
            [{"repo": repo, "branch": run.get("head_branch", ""), "run": run.get("id", 0)}],
        )
    if event == "release":
        rel = p.get("release") or {}
        return (
            f"release {rel.get('tag_name')} {p.get('action')} by {sender}",
            {"event": "release", "url": rel.get("html_url")},
            refs,
        )
    if event == "ping":
        return (f"webhook connected for {repo}", {"event": "ping"}, refs)
    return None


def ingest(
    db: Database, event: str, delivery: str, payload: dict[str, Any], notifier: Notifier | None = None
) -> dict[str, Any]:
    repo = (payload.get("repository") or {}).get("full_name")
    if not repo:
        return {"ignored": "no repository"}
    summary = summarize(event, payload)
    if summary is None:
        return {"ignored": event}
    body, data, refs = summary
    principal = _github_principal(db)
    posted = []
    for ch in channels_for_repo(db, repo):
        msg = post_message(
            db,
            principal,
            int(ch["id"]),
            {
                "kind": "event",
                "body": body,
                "data": data,
                "refs": [r for r in refs if r.get("repo")],
                "client_id": f"gh:{delivery}:{ch['id']}"[:100],
                "tags": ["github", event],
            },
            system=True,
            notifier=notifier,
        )
        posted.append({"channel": ch["name"], "id": msg["id"]})
    return {"posted": posted}
