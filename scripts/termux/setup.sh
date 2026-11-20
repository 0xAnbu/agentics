#!/data/data/com.termux/files/usr/bin/bash
# One-time setup of AgentLink on an Android phone with Termux (tested target: Xiaomi Mi 9T).
# Install Termux, Termux:Boot (and optionally Termux:API) from F-Droid, NOT Google Play.
#   pkg install -y git && git clone https://github.com/<you>/agentlink ~/agentlink
#   bash ~/agentlink/scripts/termux/setup.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
APP=$PWD

pkg update -y
pkg install -y python openssl cloudflared

python - <<'EOF'
import sqlite3, sys
assert sys.version_info >= (3, 12), f"Python >= 3.12 required, have {sys.version}"
assert sqlite3.sqlite_version_info >= (3, 35), f"SQLite >= 3.35 required, have {sqlite3.sqlite_version}"
print("python", sys.version.split()[0], "sqlite", sqlite3.sqlite_version, "OK")
EOF

python -m venv .venv
# All runtime deps are pure Python: no compiler, no Rust, no wheels for Android needed.
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# First run creates .env (mode 600) with a random admin token and prints it once.
[ -f .env ] || .venv/bin/python -c "from agentlink.serve import ensure_env; from pathlib import Path; ensure_env(Path('.env'))"

mkdir -p ~/.termux/boot
cat > ~/.termux/boot/agentlink <<EOF
#!/data/data/com.termux/files/usr/bin/bash
exec bash "$APP/scripts/termux/start.sh" >> "$APP/data/boot.log" 2>&1
EOF
chmod 700 ~/.termux/boot/agentlink
mkdir -p data && chmod 700 data

cat <<'EOF'

Setup done. Next:
  1. Android settings -> Apps -> Termux and Termux:Boot -> Battery saver: "No restrictions",
     Autostart: ON (MIUI kills background apps otherwise). Open Termux:Boot once.
  2. Public HTTPS URL (no port forwarding, works behind CGNAT):
       quick test  : bash scripts/termux/start.sh            (random *.trycloudflare.com URL)
       stable URL  : create a free Cloudflare Tunnel, then put CF_TUNNEL_TOKEN=... in .env
  3. Reboot the phone once and check data/boot.log.
EOF
