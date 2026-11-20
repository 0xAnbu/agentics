"""Self-hosted entrypoint (dev machine, VPS, Android/Termux).

    python -m agentlink.serve                 # 127.0.0.1:8080, loads .env
    python -m agentlink.serve --reload        # dev hot reload
    python -m agentlink.serve --host 0.0.0.0  # only behind a TLS proxy/tunnel

On first run without credentials it writes a .env with a random admin token
(file mode 0600) and prints it once, so a new install needs zero manual config.
"""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path


def load_env(path: Path) -> None:
    """Minimal KEY=VALUE loader (no dependency on python-dotenv). Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def ensure_env(path: Path) -> None:
    if os.environ.get("AGENTLINK_ADMIN_TOKEN") or path.exists():
        return
    token = "al_" + secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"AGENTLINK_ADMIN_TOKEN={token}\nAGENTLINK_DB_PATH=data/agentlink.db\n")
    os.environ["AGENTLINK_ADMIN_TOKEN"] = token
    print(f"\n  Created {path} with a new admin token (shown once):\n\n    {token}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run AgentLink with uvicorn")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--reload", action="store_true")
    a = ap.parse_args()
    env = Path(a.env_file)
    ensure_env(env)
    load_env(env)
    import uvicorn

    uvicorn.run(
        "agentlink.app:app",
        host=a.host,
        port=a.port,
        reload=a.reload,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        timeout_keep_alive=75,  # > long-poll wait, keeps agent connections reusable
        access_log=False,
    )


if __name__ == "__main__":
    main()
