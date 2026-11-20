"""Vercel entrypoint. vercel.json rewrites every path here; the ASGI app routes it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentlink.app import app

__all__ = ["app"]
