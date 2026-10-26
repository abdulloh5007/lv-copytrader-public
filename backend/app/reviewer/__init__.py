"""Channel reviewer: a background worker that is not one of the copytrader slots.

The ingest pool (``INGEST_WORKERS``, default 3) classifies and trades. This package
studies a Telegram source once, writes a short playbook, and keeps backups where the
signal workers cannot read them. Triage is the only copytrader path that sees the
result, and it sees only the active brief.
"""

from app.reviewer.worker import ChannelReviewer

__all__ = ["ChannelReviewer"]
