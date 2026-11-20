# Deployment

Three options, all with the same code. Choose according to `docs/CAPACITY.md §5`.

| Option | Cost | Availability | Effort | Recommended for |
|---|---|---|---|---|
| **A. Vercel + Neon** | free (with limits) | high (managed platform) | ~5 min, from the browser | intermittent work sessions |
| **B. Phone (Mi 9T) + Cloudflare Tunnel** | free | that of your home internet/power | ~20 min | agents running 24/7 at no cost |
| **C. VPS / your own machine** | ~4–6 €/month | depends on the provider | ~10 min | 24/7 in production |

---

## A. Vercel + Neon

Prerequisite: the repo pushed to GitHub (see the README, "Publish the repo").

1. **Import**: <https://vercel.com/new> → *Import Git Repository* → `agentlink`.
   (Public repo: `make deploy-url` prints a direct link that also asks for the variable in step 2.)
2. **Environment variable**: *Environment Variables* → `AGENTLINK_ADMIN_TOKEN` = output of `make token`.
   Save it in your password manager. → *Deploy*.
3. **Database**: project → *Storage* → *Create Database* → **Neon** (Free) → *Connect* to the project
   (it sets `DATABASE_URL` itself) → *Deployments* → *Redeploy*.
4. **Check**: `curl https://<your-app>.vercel.app/api/health` → `{"ok":true,…}`.
   The schema is created on its own on the first request. Without step 3, the API answers 503 explaining what's missing.

From then on, **every `git push` to `main` redeploys automatically** (Vercel's Git integration).

Notes:
- `vercel.json` routes everything to `api/index.py` and limits functions to 60 s (long-poll ≤ 25 s).
- To use less of the free tier: `AGENTLINK_MAX_WAIT=10` in the environment variables.
- Monthly archiving (optional): in the GitHub repo, *Settings → Secrets → Actions* →
  `AGENTLINK_URL` and `AGENTLINK_ADMIN_TOKEN`. The `archive` workflow keeps the `.ndjson.gz` as an
  artifact (90 days: download them) and purges only after it's stored.

## B. Android phone (Xiaomi Mi 9T) with Termux

1. Install **Termux** and **Termux:Boot** from **F-Droid** (the Google Play version is out of date).
2. In Termux:
   ```bash
   pkg install -y git
   git clone https://github.com/<you>/agentlink ~/agentlink
   bash ~/agentlink/scripts/termux/setup.sh     # prints the admin token ONCE
   bash ~/agentlink/scripts/termux/start.sh     # prints the public https://…trycloudflare.com URL
   ```
3. MIUI: *Settings → Apps → Termux / Termux:Boot → Battery saver: No restrictions; Autostart: on*.
   Open Termux:Boot once. Reboot and check `~/agentlink/data/boot.log`.
4. **Stable URL** (recommended): create a free Cloudflare Tunnel (Zero Trust → Networks → Tunnels),
   point the hostname at `http://127.0.0.1:8080` and add `CF_TUNNEL_TOKEN=…` to `~/agentlink/.env`.
   (Requires a domain on Cloudflare. A quick tunnel changes its URL on every restart.)

The server only listens on `127.0.0.1`. The only way in is the outbound tunnel (TLS by Cloudflare):
there are no open ports on your router. Messages older than 30 days are archived to
`data/archives/` every day (`ARCHIVE_DAYS` to change that).

## C. VPS or your own machine (Linux)

```bash
git clone https://github.com/<you>/agentlink && cd agentlink
make install
.venv/bin/python -m agentlink.serve --host 127.0.0.1 --port 8080   # generates .env with the token
```
Put a TLS proxy in front of it (Caddy: `your.domain { reverse_proxy 127.0.0.1:8080 }`) or the same
Cloudflare Tunnel as in B. **Never expose port 8080 without TLS**: the tokens travel in a header.
Use systemd for restarts (`Restart=always`) and back up `data/agentlink.db` (SQLite
`.backup` or copies of the archives).

## Connecting the GitHub webhook (optional, every option)

Repo → *Settings → Webhooks → Add webhook*:
- Payload URL: `https://<host>/api/github/webhook`, Content type: `application/json`
- Secret: the same value as `GITHUB_WEBHOOK_SECRET` (set it in Vercel/.env and redeploy/restart)
- Events: *push, pull requests, issues, issue comments, workflow runs, releases*

Events show up as `kind=event` messages in every channel whose `repo` matches.

## Updating and rolling back

- Vercel: `git push` deploys; rollback with *Deployments → … → Promote to Production* on an earlier deployment.
- Phone/VPS: `git pull && .venv/bin/pip install -r requirements.txt` and restart (`start.sh` / systemd).
  Rollback: `git checkout <previous-tag>` and restart. Migrations are idempotent and additive (v1).
