"""Channel reviewer: a background worker that is not one of the copytrader slots.

The ingest pool (``INGEST_WORKERS``, default 3) classifies and trades. This package
studies the active Telegram source periodically, replaces a short bounded playbook,
and keeps backups where signal workers cannot read them. Triage and trading runs
receive only the active brief for their own source.
"""

from app.reviewer.worker import ChannelReviewer

__all__ = ["ChannelReviewer"]
