# Decision log

Statuses: **accepted** (approved by the user or a standard low-impact measure),
**proposed** (awaiting approval), **rejected**.

### ADR-001 · Python + Starlette with pure-Python dependencies: accepted
- **Context**: Node isn't available in the development environment; the same code must run on Vercel,
  a VPS and Android/Termux (bionic libc, where manylinux wheels don't work).
- **Alternatives**: FastAPI (rejected: `pydantic-core` needs a Rust build on Termux);
  Node/Hono (rejected: not available locally to validate it); Go (rejected: more friction for contributors).
- **Decision**: Starlette + uvicorn + pg8000. Every dependency is a `py3-none-any` wheel (checked).
- **Consequence**: manual validation (`validation.py`) instead of pydantic schemas.

### ADR-002 · Same SQL for SQLite and Postgres: accepted
- **Context**: SQLite for dev/phone/VPS (zero ops); Postgres (Neon) on serverless, where there's no persistent disk.
- **Alternatives**: an ORM (rejected: dependency weight, opaque SQL); separate adapters per engine (rejected: duplication).
- **Decision**: a common subset (`RETURNING`, `ON CONFLICT`, epoch-ms integers, JSON as TEXT) with `?` → `%s`.
- **Consequence**: SQLite ≥ 3.35 required. The Postgres suite runs in CI (Postgres 17 service);
  it could not be run locally.

### ADR-003 · Long-polling instead of WebSockets/SSE: accepted
- **Context**: Vercel functions don't keep WebSockets open; agents are HTTP clients without a browser.
- **Alternatives**: WebSockets (rejected: not available on serverless); SSE (rejected for v1: same
  instance-time cost, harder to retry); plain polling (rejected: latency/cost).
- **Decision**: `?wait=N` (≤ 25 s, clamped to `AGENTLINK_MAX_WAIT`) + monotonic cursor `after`.
- **Consequence**: delivery in ~50 ms within one process and ≤ `AGENTLINK_POLL_INTERVAL` across instances.

### ADR-004 · Bearer tokens hashed with SHA-256: accepted
- **Context**: credentials for agents (machines), not human passwords.
- **Alternatives**: Argon2id (rejected: the guide recommends it for *passwords*; a 256-bit random token can't be
  brute-forced and would add ~50 ms per request); OAuth/SSO (rejected: out of the agreed scope).
- **Decision**: `al_` + 32 random bytes; only `SHA-256(token)` is stored; revocation, expiry, rotation.
- **Consequence**: whoever gets hold of a database copy can't use the tokens.

### ADR-005 · Bootstrap admin via an environment variable: accepted
- **Context**: minimal intervention on deploy, with no "first visitor becomes admin" race.
- **Alternatives**: one-time `/setup` (rejected: race condition on a public URL); token in the logs (rejected: secrets in logs).
- **Decision**: `AGENTLINK_ADMIN_TOKEN`. Locally/on the phone `serve.py` generates it into `.env` (0600) on the first run.

### ADR-006 · Retention: verified archive before deleting: accepted
- **Context**: messages must be kept, without filling up the server (Neon free = 1 GB).
- **Decision**: export NDJSON.gz → on-disk verification → the server recomputes count + SHA-256 → only then deletes.
- **Rejected**: deleting by TTL (loses information); compacting `system` messages (left for the future).

### ADR-007 · Deployment: Vercel + Neon as the primary target, phone/VPS as the 24/7 alternative: accepted
- **Context**: the user asked for Vercel/GitHub and a phone (Mi 9T) as an option.
- **Decision**: both supported with the same code. GitHub Pages rejected: static only, it can't validate credentials.
- **Consequence**: the Vercel/Neon free tier doesn't cover agents connected 24/7 (see `docs/CAPACITY.md §5.1`).
- **Limitation**: the deployment couldn't be automated end to end without GitHub/Vercel credentials;
  2–3 manual steps remain (see `docs/DEPLOY.md`).

### ADR-008 · Fixed roles (admin/member/monitor) and per-channel membership: accepted
- **Decision**: members can create channels and invite; only admins manage agents/tokens and remove others.
  `monitor` reads everything and writes nothing (the human monitoring UI).
- **Proposed (pending)**: per-channel permissions (read-only for some agents), scopes per token.

### ADR-009 · Admin-only deletion of messages and channels: accepted
- **Context**: humans need to remove mistakes, leaked text or abandoned channels; ADR-006 only covers age-based purges.
- **Decision**: `DELETE /api/admin/messages/{id}` and `DELETE /api/admin/channels/{name}`, `admin` role only,
  exposed in `./admin` and as buttons in the web UI shown only to admin tokens. Every deletion is audited.
  A channel deletion removes its messages, mentions, tasks, leases, KV and memberships.
- **Agents** (`DELETE /api/admin/agents/{name}`, `./admin users remove`): tokens, memberships, leases, presence and
  mentions are deleted; unfinished tasks go back to `open`. The row stays as a disabled tombstone renamed
  `"<name> (deleted #<id>)"` (a name `NAME_RE` can't produce) so history keeps a readable author and the name is
  free again. `?purge_messages=1` also deletes the agent's messages. The bootstrap admin and yourself can't be deleted.
- **Not available** to `member` (agents) or `monitor`, not exposed through MCP. Not reversible: archive first if in doubt.
