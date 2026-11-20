"""Print the Vercel import URL for this repository's GitHub origin."""

from __future__ import annotations

import re
import subprocess
import sys
from urllib.parse import quote


def main() -> None:
    try:
        origin = subprocess.check_output(["git", "remote", "get-url", "origin"], text=True).strip()  # noqa: S607
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("No git remote 'origin'. Run: git remote add origin https://github.com/<you>/agentlink.git")
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", origin)
    if not m:
        sys.exit(f"origin is not a GitHub URL: {origin}")
    repo = f"https://github.com/{m.group(1)}/{m.group(2)}"
    print(
        "https://vercel.com/new/clone?repository-url="
        + quote(repo, safe="")
        + "&project-name=agentlink&env=AGENTLINK_ADMIN_TOKEN"
        + "&envDescription="
        + quote("Admin token: run `make token` (al_ + random). Keep it secret.")
    )
    print("\nPrivate repo? Use instead: https://vercel.com/new -> Import -> pick the repo.", file=sys.stderr)


if __name__ == "__main__":
    main()
