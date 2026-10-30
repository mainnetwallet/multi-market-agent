#!/usr/bin/env python
"""Create a local .env with strong admin/session secrets if one is absent.

Never overwrites an existing .env. Prints only where the password is stored.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
MARKER = "change-me-now"
SESSION_MARKER = "change-me-to-a-long-random-stable-string"


def main() -> int:
    if ENV.exists():
        print(".env already exists; refusing to overwrite it.")
        return 1
    if not EXAMPLE.exists():
        print(".env.example is missing.", file=sys.stderr)
        return 2
    text = EXAMPLE.read_text(encoding="utf-8")
    password = secrets.token_urlsafe(24)
    session_secret = secrets.token_urlsafe(48)
    if MARKER not in text or SESSION_MARKER not in text:
        print(".env.example markers changed; refusing to generate blindly.", file=sys.stderr)
        return 3
    text = text.replace(f"ADMIN_PASSWORD={MARKER}", f"ADMIN_PASSWORD={password}")
    text = text.replace(f"SESSION_SECRET={SESSION_MARKER}", f"SESSION_SECRET={session_secret}")
    ENV.write_text(text, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(ENV, 0o600)
    print("Created .env with fresh ADMIN_PASSWORD and SESSION_SECRET.")
    print("Admin username: operator")
    print("The password is in .env under ADMIN_PASSWORD. Keep the file private.")
    print("Next: alembic upgrade head && python scripts/doctor.py --venue all && python -m mm_agent.main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
