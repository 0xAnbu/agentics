"""Measure real on-disk bytes per message and project accumulated storage.

Inserts a realistic mix of agent traffic through the actual service layer
(same SQL, indexes, mentions, system messages) into a temporary SQLite DB,
then measures size and the gzip ratio of the archive format.

    python scripts/estimate_storage.py --messages 20000
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentlink import archive, coordination, directory, messages
from agentlink.config import Settings
from agentlink.hub import build_hub
from agentlink.security import Principal, ensure_admin_agent

WORDS = [
    "update",
    "refactor",
    "parser",
    "config",
    "tests",
    "pass",
    "fail",
    "lint",
    "ci",
    "branch",
    "merge",
    "rebase",
    "review",
    "api",
    "endpoint",
    "schema",
    "migration",
    "auth",
    "token",
    "cache",
    "retry",
    "timeout",
    "handler",
    "module",
    "function",
    "class",
    "bug",
    "fix",
    "feature",
    "docs",
    "deploy",
]


def sentence(lo: int, hi: int) -> str:
    return " ".join(random.choice(WORDS) for _ in range(random.randint(lo, hi)))


SCENARIOS = {
    # messages per 24 h for 5 main agents (+ sub-agents), see docs/CAPACITY.md for the assumptions
    "light (5 agents, 8 h/day, ~1 msg / 10 min each)": 5 * 8 * 6 + 50,
    "typical (5 agents + sub-agents, 12 h/day, ~1 msg / 2 min)": 5 * 12 * 30 + 300,
    "intense (5 agents + 15 sub-agents, 24 h, ~1 msg / min each)": 20 * 24 * 60,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", type=int, default=20000)
    a = ap.parse_args()
    random.seed(7)
    work = Path(tempfile.mkdtemp())
    hub = build_hub(Settings(admin_token="al_x", sqlite_path=str(work / "est.db"), write_rate_per_min=0))
    db = hub.db
    admin = Principal(ensure_admin_agent(db), "admin", "admin", None)
    names = [f"agent{i}" for i in range(5)] + [f"sub{i}" for i in range(15)]
    for n in names:
        directory.create_agent(db, admin, {"name": n})
    directory.create_channel(db, admin, {"name": "dev", "repo": "acme/app", "members": names})
    people = {n: Principal(int(directory.agent_by_name(db, n)["id"]), n, "member", None) for n in names}

    kinds = ["status"] * 40 + ["chat"] * 25 + ["question", "answer"] * 6 + ["review"] * 8 + ["decision"] * 3
    kinds += ["handoff"] * 2 + ["alert"] * 2
    created = 0
    while created < a.messages:
        who = people[random.choice(names)]
        r = random.random()
        if r < 0.05:  # task + claim -> 2 messages
            t = coordination.create_task(db, who, "dev", {"title": sentence(3, 8), "description": sentence(10, 40)})
            coordination.claim_task(db, people[random.choice(names)], int(t["id"]))
            created += 2
        elif r < 0.08:  # lease -> 1 system message
            try:
                coordination.acquire_lease(db, who, "dev", {"resource": f"src/m{random.randint(0, 500)}.py"})
                created += 1
            except Exception:  # noqa: S110 - conflicts are expected traffic
                pass
        else:
            body = sentence(8, 60)
            if random.random() < 0.15:
                body += " @" + random.choice(names)
            payload = {
                "kind": random.choice(kinds),
                "body": body,
                "refs": [
                    {"repo": "acme/app", "path": f"src/m{random.randint(0, 99)}.py", "line": random.randint(1, 900)}
                ]
                if random.random() < 0.5
                else None,
                "data": {
                    "tests": random.randint(0, 300),
                    "branch": f"feat/{random.randint(1, 99)}",
                    "files": [f"src/m{random.randint(0, 99)}.py" for _ in range(random.randint(0, 4))],
                }
                if random.random() < 0.4
                else None,
            }
            messages.post_message(db, who, "dev", payload)
            created += 1
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    st = archive.stats(db, admin)
    total = st["messages_total"]
    size = st["db_bytes"]
    per_msg = size / total
    raw = "".join(archive.export_lines(db, admin, 10**12)).encode()
    gz = gzip.compress(raw, compresslevel=6)
    out = {
        "messages": total,
        "db_bytes": size,
        "db_bytes_per_message": round(per_msg, 1),
        "avg_payload_chars": st["avg_payload_chars"],
        "archive_ndjson_bytes_per_message": round(len(raw) / total, 1),
        "archive_gzip_bytes_per_message": round(len(gz) / total, 1),
        "gzip_ratio": round(len(raw) / len(gz), 2),
        "projection_24h": {
            name: {
                "messages": n,
                "db_MB": round(n * per_msg / 1e6, 2),
                "db_MB_30d": round(30 * n * per_msg / 1e6, 1),
                "archive_gz_MB_30d": round(30 * n * len(gz) / total / 1e6, 2),
            }
            for name, n in SCENARIOS.items()
        },
    }
    print(json.dumps(out, indent=2))
    print("\nexample record:", json.loads(raw.split(b"\n")[3]), file=sys.stderr)


if __name__ == "__main__":
    main()
