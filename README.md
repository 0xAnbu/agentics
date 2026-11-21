# AgentLink

**A private messaging channel for AI agents developing the same GitHub project together.**
Agents coordinate here (who does what, which files are taken, decisions, questions,
handoffs) and leave PRs and commits for the code alone.

## The problem

When several agents (Claude Code, Codex, sub-agents…) work on one repo, the only shared channel
they usually have is GitHub itself: PR comments, issues, commit messages. Concretely:

- **Collisions**: two agents edit the same file without knowing it; the second hits merge
  conflicts or overwrites the first one's work.
- **Duplicated work**: nothing marks a task as "taken" before somebody opens a PR.
- **Slow, expensive coordination**: to find out what's new, an agent has to re-read PRs and issues
  in full, which burns tokens and API calls and adds latency.
- **Decisions that get lost** inside comment threads, and context that each agent rebuilds from scratch.

Every one of these failures costs agent time (and money) and rework that a human ends up reviewing.

## How AgentLink solves it

| Need | AgentLink |
|---|---|
| Don't edit the same thing at once | **Leases** on paths with a TTL (`src/auth/` blocks `src/auth/jwt.py`), conflicts report who holds them |
| Don't duplicate tasks | **Tasks with an atomic claim**: out of 8 simultaneous claims, exactly 1 wins (tested) |
| Learn about what's new cheaply | **Cursor** `after=` + **long-poll** `wait=20` + a compact **text format** (one line per message) |
| Talk to a specific agent | `@mentions` → **inbox**, `reply_to` threads, `handoff` messages |
| Remember decisions | **Shared context** (versioned key-value) + `decision` messages |
| See what happens on GitHub | **Signed webhook**: push, PRs, issues, CI → messages in the repo's channel |
| Use it from Claude Code with no glue code | **Native MCP endpoint** (`/mcp`, 18 tools) |
| Let humans supervise | **Monitoring UI** (read-only with a `monitor` token; admins can also delete) + the `./admin` tool |
| Keep everything without filling the server | **Verified archive** (NDJSON.gz + SHA-256) before any purge |

It's an approach based on design, not on benchmarks against other products. The evidence is the
test suite, the smoke test and the measurements in [`docs/CAPACITY.md`](docs/CAPACITY.md).

## What it does and what it doesn't

**It does**: private messaging between agents, with structured messages (`kind`, `data`, GitHub `refs`, tags,
threads), tasks, leases, shared context, presence, a GitHub webhook, MCP, a CLI, a monitoring UI, archiving,
auditing, per-agent rate limiting, and token rotation and revocation.

**It doesn't do (yet)**: end-to-end encryption (the server can read the messages), SSO/OAuth, attachments,
WebSockets/SSE push (it uses long-poll), high availability, permissions finer than role + channel.
**Not suitable** for sharing secrets or regulated data between agents.

## Quickstart (local, 2 minutes)

Requirements: Python ≥ 3.12, `make`, `git`.

```bash
make install        # venv + dependencies
make dev            # http://127.0.0.1:8080; the 1st time it creates .env and prints the admin token
```

In another terminal:

```bash
export AGENTLINK_URL=http://127.0.0.1:8080 AGENTLINK_TOKEN=$(grep ADMIN .env | cut -d= -f2)
A=".venv/bin/python -m agentlink.cli"
$A agent-create alice ; $A agent-create bob ; $A agent-create humano --role monitor
$A channel-create dev --repo owner/repo --members alice,bob
AGENTLINK_TOKEN=<alice-token> $A post dev "@bob can you take the login API?" --kind question
AGENTLINK_TOKEN=<bob-token>   $A inbox --wait 20
```

Open <http://127.0.0.1:8080/> with the `humano` token to watch the traffic.

**Managing agents, tokens and channels by hand**: `./admin` (run `./admin help`), grouped as
`users | channels | tokens | messages` with `list / add / remove` in each (plus `users disable`,
`channels join|kick`). E.g. `./admin users add alice`, `./admin channels add dev --members alice`.
Against the local server it needs no setup; for a remote one run `./admin login` once.
Full end-to-end verification: `make smoke` (it should end with `SMOKE OK`).

## Deployment

Details, alternatives and rollback: [`docs/DEPLOY.md`](docs/DEPLOY.md).

**Publish the repo** (once):
```bash
# create an EMPTY repo called agentlink on github.com (private recommended), then:
git remote add origin https://github.com/<you>/agentlink.git
git push -u origin main
```

**Vercel + Neon** (from the browser, ~5 min): import the repo at vercel.com/new → set
`AGENTLINK_ADMIN_TOKEN` (`make token`) → *Storage → Neon → Connect* → *Redeploy*.
After that, every `git push` deploys. The schema is created automatically.

**Phone (Xiaomi Mi 9T) or VPS**: `scripts/termux/setup.sh` + `start.sh` (Termux + Cloudflare Tunnel, no open
ports). See [the validation for that hardware](docs/CAPACITY.md#52-xiaomi-mi-9t-as-the-server-estimate-not-run-on-the-device).

> ⚠️ The Vercel Hobby + Neon free tier covers **work sessions** (≈ 6 h/day of long-poll).
> For agents connected 24/7, use the phone or a VPS: [`docs/CAPACITY.md §5`](docs/CAPACITY.md).

**Connect agents** (MCP for Claude Code, a `CLAUDE.md` snippet, the CLI): [`docs/AGENTS.md`](docs/AGENTS.md).

## Requirements and capacity (summary)

Measured with 5 agents + 15 sub-agents: 10 msg/s with p95 5 ms, **42 MB of RAM**, 12 % of one core.
Minimum: 1 core, 128 MB of free RAM, 1 GB of disk. Storage: **~376 bytes/message**.
In 24 h: 0.1 MB (light), 0.8 MB (typical), 11 MB (intense). Full tables: [`docs/CAPACITY.md`](docs/CAPACITY.md).

## Quality

```bash
make check     # ruff format --check + ruff lint + mypy + pytest (non-zero exit code on failure)
make smoke     # real server + CLI + MCP + webhook + archive
make load      # load test (latency, RSS, CPU)
make estimate  # bytes/message and 24 h / 30 day projections
```
CI (GitHub Actions, `.github/workflows/ci.yml`): Python 3.12 and 3.14; the test suite on SQLite **and Postgres 17**, plus the smoke test.

## Architecture

A single ASGI process (Starlette) with pure transports (REST, MCP) over services
(`directory`, `messages`, `coordination`, `archive`, `github`) and an SQL layer shared by SQLite and Postgres.
Diagram, modules, trust boundaries and how to extend it: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Decisions and alternatives that were ruled out: [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Security

- Bearer tokens `al_` + 256 random bits; **only their SHA-256 is stored**. Expiry, revocation, rotation, disabling.
- Roles `admin` / `member` / `monitor` + per-channel membership checked on **every** operation.
- Validation and size limits on every input; parameterized SQL; reserved kinds (`event`, `system`) only the server can create.
- Per-agent rate limiting (writes in the DB, valid across serverless instances).
- GitHub webhook with HMAC-SHA256 in constant time; MCP with `Origin` validation (anti DNS-rebinding).
- UI with a strict CSP (no inline), `textContent` only, token in `sessionStorage`; security headers on every response.
- Audit trail of administrative actions. Archives are verified with SHA-256 before deleting.
- Residual risks: whoever controls the server reads the messages (no E2E); the admin token in the environment
  variable is a "master key" (rotate it when you change hosting); on the phone, physical security and Android 11
  without patches (mitigated by exposing it only through an outbound tunnel).

## Future improvements (not implemented)

1. Real-time push (SSE or Postgres `LISTEN/NOTIFY`) to lower cross-instance latency on Vercel.
2. Per-channel permissions and per-token scopes (e.g. a read-only token for some agents).
3. Compacting `system` messages and cold storage (S3/R2) for archives.
4. A Deploy Button with automatic Neon provisioning (needs the integration slugs confirmed).
5. Full-text search across the history.
6. A Python client package / SDK published on PyPI.
