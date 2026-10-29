"""Reset local dashboard access using Docker-owner access and a secret on stdin."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["password"])
    parser.parse_args()
    from app.config import settings
    from app.util.admin_credentials import AdminCredentials
    try:
        raw = sys.stdin.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("Input too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("password"), str):
            raise ValueError("Expected a password object on stdin")
        username = payload.get("username")
        if username is not None and not isinstance(username, str):
            raise ValueError("Invalid username")
        credentials = AdminCredentials(Path(settings.DATA_DIR) / "admin-credentials.json",
                                       lambda: (settings.SP_AUTH_USER, settings.SP_AUTH_PASSWORD))
        credentials.change(payload["password"], username)
    except (ValueError, OSError, RuntimeError):
        print("Password reset refused: check input and writable, valid auth state.", file=sys.stderr)
        return 1
    print("Dashboard password updated. Previous sessions are invalid; trading was not restarted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
