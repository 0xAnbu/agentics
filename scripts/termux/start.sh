#!/data/data/com.termux/files/usr/bin/bash
# Start AgentLink + Cloudflare Tunnel on Termux and keep both alive.
# The server only listens on 127.0.0.1; the tunnel is the only way in (TLS by Cloudflare).
set -uo pipefail
cd "$(dirname "$0")/../.."
mkdir -p data
command -v termux-wake-lock >/dev/null && termux-wake-lock   # keep CPU awake with screen off

CF_TUNNEL_TOKEN=$(grep -E '^CF_TUNNEL_TOKEN=' .env 2>/dev/null | cut -d= -f2- || true)

supervise() {  # restart a command forever with backoff; logs to data/<name>.log
  local name=$1; shift
  while true; do
    echo "$(date -Is) starting $name" >> "data/$name.log"
    "$@" >> "data/$name.log" 2>&1
    echo "$(date -Is) $name exited ($?), restarting in 5s" >> "data/$name.log"
    sleep 5
  done
}

supervise server .venv/bin/python -m agentlink.serve --host 127.0.0.1 --port 8080 &
if [ -n "$CF_TUNNEL_TOKEN" ]; then
  supervise tunnel cloudflared tunnel --no-autoupdate run --token "$CF_TUNNEL_TOKEN" &
else
  supervise tunnel cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080 &
  sleep 8
  echo "Public URL (changes on restart):"
  grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' data/tunnel.log | tail -1
fi

# Daily verified archive of messages older than 30 days (keeps the live DB small).
(
  while true; do
    sleep 86400
    TOKEN=$(grep -E '^AGENTLINK_ADMIN_TOKEN=' .env | cut -d= -f2-)
    AGENTLINK_TOKEN=$TOKEN .venv/bin/python -m agentlink.cli --url http://127.0.0.1:8080 \
      archive --older-than-days "${ARCHIVE_DAYS:-30}" --out data/archives --prune >> data/archive.log 2>&1
  done
) &
wait
