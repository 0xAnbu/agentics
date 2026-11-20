"""Load test against a real uvicorn process.

Simulates N main agents (each long-polling its inbox and the feed) plus M
sub-agents posting status/messages, for a fixed duration. Reports throughput,
latency percentiles and the server's peak RSS / CPU — the numbers used for the
minimum server requirements in docs/CAPACITY.md.

    python scripts/loadtest.py --agents 5 --subagents 15 --seconds 60
"""

from __future__ import annotations

import argparse
import json
import os
import random
import secrets
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def req(url: str, token: str, method: str = "GET", body: dict | None = None, timeout: float = 60) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - local URL
    r.add_header("Authorization", f"Bearer {token}")
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, {}


def proc_stats(pid: int) -> tuple[int, float]:
    rss = 0
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1]) * 1024
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    cpu = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    return rss, cpu


BODIES = [
    "Starting work on {f}; will push to feat/{n} in ~20 min.",
    "Tests for {f} pass locally (42 passed). Opening PR.",
    "@{peer} I changed the signature of parse_config(); please rebase before touching {f}.",
    "Blocked: CI fails on lint in {f}, investigating.",
    "Decision: we keep SQLite for dev and Postgres in prod; migrations stay plain SQL.",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=5)
    ap.add_argument("--subagents", type=int, default=15)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--post-interval", type=float, default=2.0, help="mean seconds between posts per agent")
    a = ap.parse_args()

    work = tempfile.mkdtemp()
    port = 21000 + random.randint(0, 9000)
    admin = "al_" + secrets.token_urlsafe(32)
    env = {
        **os.environ,
        "AGENTLINK_ADMIN_TOKEN": admin,
        "AGENTLINK_DB_PATH": f"{work}/load.db",
        "AGENTLINK_WRITE_RATE": "100000",
        "AGENTLINK_READ_RATE": "1000000",
        "AGENTLINK_LOG_LEVEL": "WARNING",
    }
    server = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "agentlink.serve", "--port", str(port), "--env-file", f"{work}/none.env"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1)  # noqa: S310
                break
            except OSError:
                time.sleep(0.1)
        rss0, cpu0 = proc_stats(server.pid)
        names = [f"agent{i}" for i in range(a.agents)] + [f"sub{i}" for i in range(a.subagents)]
        tokens = {}
        for n in names:
            tokens[n] = req(base + "/api/admin/agents", admin, "POST", {"name": n})[1]["token"]
        req(base + "/api/channels", admin, "POST", {"name": "dev", "repo": "acme/app", "members": names})

        lat_post: list[float] = []
        lat_deliver: list[float] = []
        errors = {"n": 0}
        sent: dict[int, float] = {}
        peak = {"rss": rss0}
        stop = time.monotonic() + a.seconds
        lock = threading.Lock()

        def poster(name: str) -> None:
            i = 0
            while time.monotonic() < stop:
                time.sleep(random.expovariate(1 / a.post_interval))
                body = random.choice(BODIES).format(f=f"src/mod{i % 7}.py", n=i, peer=random.choice(names))
                payload = {
                    "body": body,
                    "kind": random.choice(["status", "chat", "status", "review", "question"]),
                    "refs": [{"repo": "acme/app", "path": f"src/mod{i % 7}.py"}],
                    "data": {"step": i, "tests": random.randint(0, 200)},
                }
                t0 = time.monotonic()
                code, msg = req(base + "/api/channels/dev/messages", tokens[name], "POST", payload)
                dt = time.monotonic() - t0
                with lock:
                    if code == 200:
                        lat_post.append(dt)
                        sent[msg["id"]] = t0 + dt
                    else:
                        errors["n"] += 1
                i += 1

        def listener(name: str) -> None:
            after = 0
            while time.monotonic() < stop:
                code, res = req(base + f"/api/feed?after={after}&wait=20&limit=200", tokens[name])
                now = time.monotonic()
                if code != 200:
                    errors["n"] += 1
                    continue
                for m in res["messages"]:
                    with lock:
                        if m["id"] in sent:
                            lat_deliver.append(now - sent[m["id"]])
                after = res["next_after"]
                req(base + "/api/inbox?after=0&limit=5", tokens[name])

        def sampler() -> None:
            while time.monotonic() < stop:
                peak["rss"] = max(peak["rss"], proc_stats(server.pid)[0])
                time.sleep(0.5)

        threads = [threading.Thread(target=poster, args=(n,)) for n in names]
        threads += [threading.Thread(target=listener, args=(n,)) for n in names[: a.agents]]
        threads.append(threading.Thread(target=sampler))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=a.seconds + 40)
        _, cpu1 = proc_stats(server.pid)
        db_bytes = sum(p.stat().st_size for p in Path(work).glob("load.db*"))

        def pct(xs: list[float], q: float) -> float:
            return round(statistics.quantiles(xs, n=100)[int(q) - 1] * 1000, 1) if len(xs) > 2 else 0.0

        print(
            json.dumps(
                {
                    "agents": a.agents,
                    "subagents": a.subagents,
                    "seconds": a.seconds,
                    "messages_posted": len(lat_post),
                    "errors": errors["n"],
                    "posts_per_sec": round(len(lat_post) / a.seconds, 2),
                    "post_latency_ms": {"p50": pct(lat_post, 50), "p95": pct(lat_post, 95), "p99": pct(lat_post, 99)},
                    "delivery_latency_ms": {"p50": pct(lat_deliver, 50), "p95": pct(lat_deliver, 95)},
                    "server_rss_mb": {"idle": round(rss0 / 2**20, 1), "peak": round(peak["rss"] / 2**20, 1)},
                    "server_cpu_seconds": round(cpu1 - cpu0, 2),
                    "server_cpu_pct_of_one_core": round((cpu1 - cpu0) / a.seconds * 100, 1),
                    "db_bytes": db_bytes,
                },
                indent=2,
            )
        )
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main()
