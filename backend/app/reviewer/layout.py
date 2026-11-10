"""Directory names for the channel brief. No I/O.

Copytrader code may open the active brief and its source marker, and nothing under ``reviewer/`` or
``backups/``. The names live here so the loader and the archive cannot drift apart.
"""

from __future__ import annotations

ROOT_DIR: str = "channel-briefs"
ACTIVE_DIR: str = "active"
REVIEWER_DIR: str = "reviewer"
BACKUP_DIR: str = "backups"
CURRENT_DIR: str = "current"
BRIEF_FILE: str = "brief.md"
IDENTITY_FILE: str = "source.json"
META_FILE: str = "meta.json"
STUDY_FILE: str = "study.json"

#: Path segments a signal worker is never allowed to open.
HIDDEN_FROM_COPYTRADER: frozenset[str] = frozenset({REVIEWER_DIR, BACKUP_DIR})
