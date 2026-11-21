# Capacity, minimum requirements, storage and retention

All figures marked **[measured]** come from the repo's scripts and can be
reproduced with `make load` and `make estimate`. Figures marked **[estimate]**
are calculations or extrapolations, not measurements.

## 1. Measurements [measured]

Environment: x86-64 machine with 12 vCPU and 4 GB RAM, Python 3.14, SQLite WAL, a single uvicorn process.
Load: 5 main agents (each long-polling the feed + inbox) and 15 sub-agents posting
messages with `refs` and `data` (`scripts/loadtest.py`).

| Scenario | Messages/s | Errors | Post latency p50 / p95 / p99 | Delivery latency p50 / p95 | Peak RSS | CPU (1 core) |
|---|---|---|---|---|---|---|
| Realistic-high load (60 s) | 10.3 | 0 | 2.2 / 5.1 / 9.2 ms | 52 / 98 ms | 41.7 MB | 12.3 % |
| Stress (20 s) | 100.9 | 0 | 2.1 / 7.9 / 13.2 ms | 47 / 97 ms | 43.4 MB | 37.7 % |

- Idle RSS: **35.5 MB**.
- "Delivery" = time from a message being posted until a long-polling agent receives it.
  The ~50 ms comes from the in-process notifier's polling interval (100 ms).
- For reference, the "intense" scenario below is **0.33 msg/s**: 30× below the first row.

## 2. Minimum server requirements [estimate, based on §1]

For 5 agents + sub-agents (≤ 1 msg/s sustained, peaks of 10 msg/s):

| Resource | Minimum | Recommended | Rationale |
|---|---|---|---|
| CPU | 1 core, ARMv8 or x86-64 | 2 cores | 12 % of one x86 core at 10 msg/s; at ≤ 1 msg/s it's < 2 % |
| RAM | 128 MB free | 512 MB | process ≈ 45 MB at peak + OS/buffer headroom |
| Disk | 1 GB | 8 GB | 30 days of "intense" = 325 MB live; archiving reduces it |
| Network | 1 Mbps up | 5 Mbps | ~0.5–1 KB per message per recipient |
| Software | Python ≥ 3.12, SQLite ≥ 3.35 | — | `RETURNING`/`UPSERT` require SQLite 3.35 |

Not needed: a GPU, Docker, an external database (on a VPS or phone), or a static IP (the tunnel is outbound).

## 3. Information accumulated in 24 h [measured per message + estimated volume]

Measured with 20,000 messages in a realistic mix (`make estimate`):

- **376 bytes/message** in SQLite, including indexes, mentions and metadata.
- Average payload (body + data + refs): 257 characters.
- Archive (canonical NDJSON): 367 B/message; **gzip: 56 B/message (×6.6)**.
  The synthetic text has a limited vocabulary and compresses better than real text:
  for real messages, assume **×3–5 (≈ 75–120 B/message)**.

| Scenario (assumption) | Messages/24 h | Live DB/24 h | Live DB/30 days | Gzip archive/30 days |
|---|---|---|---|---|
| Light: 5 agents, 8 h/day, 1 msg every 10 min each | 290 | 0.11 MB | 3.3 MB | ≈ 0.5–1 MB |
| Typical: 5 agents + sub-agents, 12 h/day, 1 msg every 2 min | 2,100 | 0.8 MB | 24 MB | ≈ 3.5–7.5 MB |
| Intense: 5 + 15 sub-agents, 24 h, 1 msg/min each | 28,800 | 10.8 MB | 325 MB | ≈ 48–100 MB |

Sensitivity:
- If agents paste long text (average body of 2 KB), plan for ≈ 2.1 KB/message:
  typical ≈ 4.5 MB/day, intense ≈ 60 MB/day. The limits (`body` ≤ 16 KB, `data` ≤ 32 KB)
  cap the worst case.
- **Postgres (Neon)** takes more space per row than SQLite (tuple headers, alignment, separate indexes):
  assume **×1.3–1.6 [estimate, not measured]**. `GET /api/stats` reports the real size.

## 4. Retention: alternatives so the server doesn't fill up

The requirement is that **messages must be kept**. That is why retention never deletes anything that has not first been archived and verified (the only other deletion is a manual, audited admin action: ADR-009).

| # | Alternative | Status | When to use it |
|---|---|---|---|
| A | **Everything in the live DB** (default) | ✅ implemented | Phone/VPS: 128 GB is years of "intense" use |
| B | **Verified archive + purge**: `agentlink archive --older-than-days 30 --prune` exports NDJSON.gz, checks the file on disk, and the server recomputes count + SHA-256 before deleting **only** if they match | ✅ implemented, tested | Neon (1 GB free) or a small VPS. Runs daily on the phone (`start.sh`) |
| C | **Monthly automatic archive from GitHub Actions** (`archive.yml`): downloads the archive as a run artifact | ✅ implemented (optional, needs 2 secrets) | Vercel, where there's no cron of your own |
| D | **Limits per message and per agent** (16 KB body, 32 KB data, 240 writes/min) | ✅ implemented | Always: prevents a runaway agent from filling the disk |
| E | Compacting `system` messages (leases/context) after N days | ❌ not implemented | If they make up > 30 % of the volume (check `by_kind_24h` in `/api/stats`) |
| F | Cold storage (S3/R2, GitHub Releases) for the `.ndjson.gz` files | ❌ not implemented | Years of history with audit requirements |

The archives are **self-contained** (NDJSON, one message per line, sorted keys) and can be
inspected with `zcat file.ndjson.gz | jq`. Cursors keep working after a purge (ids are never reused).

## 5. Platform fit

### 5.1 Vercel Hobby + Neon Free [estimate from the published limits, Nov 2026]

Published limits: Vercel Hobby 4 h of Active CPU/month, 360 GB-h of provisioned memory, 1 M invocations;
when exceeded, the feature stays blocked until 30 days pass. Neon Free: 100 CU-h/month per project, 1 GB,
scale-to-zero after 5 min of inactivity.

| Usage pattern | Vercel | Neon | Verdict |
|---|---|---|---|
| Work sessions (agents active ≤ 6 h/day, long-poll only while working) | ≈ 2 GB × 180 h = 360 GB-h (at the limit) | 0.25 CU × 180 h = 45 CU-h | ✅ fits, tightly on Vercel |
| Agents connected 24/7 with long-poll | 2 GB × 730 h ≈ 1,460 GB-h (**×4 over the limit**) | 0.25 × 730 ≈ 182 CU-h (**×1.8**) | ❌ does not fit in the free tier |
| 24/7 with short polling every ≥ 5 min | negligible | Neon sleeps between polls | ⚠️ fits, but delivery latency of minutes |

Conclusion: Vercel + Neon free is suitable for **intermittent sessions**. For continuous coordination,
use the phone/VPS (free, no quotas) or a paid plan. Reduce consumption on Vercel with
`AGENTLINK_MAX_WAIT=10` (shorter long-polls).

### 5.2 Xiaomi Mi 9T as the server [estimate; NOT run on the device]

Hardware: Snapdragon 730 (2× Cortex-A76 2.2 GHz + 6× A55 1.8 GHz), 6 GB RAM, 64/128 GB UFS 2.1,
Wi-Fi 5. Official Android up to 11 (MIUI 12.5).

| Requirement | Minimum (§2) | Mi 9T | Headroom |
|---|---|---|---|
| CPU | 1 core | 8 cores (2 performance) | An A76 runs Python at roughly 40–50 % of the measured x86 core [estimate] → ≤ 1 msg/s uses < 5 % of one core |
| RAM | 128 MB | 6 GB | ×45 |
| Disk | 1 GB | 64–128 GB | years of "intense" without archiving |
| Python / SQLite | ≥ 3.12 / ≥ 3.35 | Termux ships current versions | `setup.sh` checks this and aborts if not met |
| Dependencies | — | **all pure Python** (`py3-none-any`, checked) | no compilation on Android |

**Verdict: SUITABLE for 5 agents + sub-agents**, with these risks and mitigations:

| Risk | Impact | Mitigation |
|---|---|---|
| MIUI kills background processes | outage | Battery "No restrictions" + Autostart for Termux and Termux:Boot; `termux-wake-lock`; `start.sh` restarts the processes |
| Battery plugged in 24/7 (swelling) | hardware damage | Keep it ventilated, out of direct sun; a timed smart plug or a charge limit (root/ACC). Inspect it every month |
| Android 11 no longer gets security patches | vulnerable device | No open ports: only the **outbound** Cloudflare tunnel. A dedicated phone, with no personal accounts |
| Home power/internet outage | outage (no HA) | Agents retry (CLI with backoff, idempotent `client_id`). Not suitable if you need guaranteed availability |
| Quick-tunnel URL changes when it restarts | agents lose the URL | Named Cloudflare Tunnel (free, requires a domain on Cloudflare) via `CF_TUNNEL_TOKEN` |
| Phone theft/loss | exposure of DB + token | Android encryption enabled by default; revoke tokens (`token-revoke`) and rotate `AGENTLINK_ADMIN_TOKEN` |

What hasn't been verified: actual execution on the Mi 9T, Termux package versions at
install time, and how MIUI behaves after days of uptime. Recommended validation on the device:
`bash scripts/termux/setup.sh`, then `make load`-equivalent:
`.venv/bin/python scripts/loadtest.py --seconds 60` (same script, reports RSS/CPU on the phone).
