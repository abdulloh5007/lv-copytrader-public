"""The only channel-brief reader the copytrader and triage paths may use.

:func:`load_active_brief` opens one file: ``<DATA_DIR>/channel-briefs/active/brief.md``.
With a chat ID it also checks the active source marker. It does not list directories,
does not follow a symlink out of the active files, and raises
rather than returning anything that lives under ``backups/`` or ``reviewer/``.
"""

from __future__ import annotations

from pathlib import Path
import json

from app.reviewer.layout import (
    ACTIVE_DIR,
    BRIEF_FILE,
    HIDDEN_FROM_COPYTRADER,
    IDENTITY_FILE,
    ROOT_DIR,
)


class BriefIsolationError(RuntimeError):
    """A copytrader path tried to read a reviewer archive or study artifact."""


def active_brief_path(data_dir: str | Path) -> Path:
    """Return the single file triage is allowed to inject."""
    return Path(data_dir) / ROOT_DIR / ACTIVE_DIR / BRIEF_FILE


def assert_copytrader_readable(path: Path, data_dir: str | Path) -> Path:
    """Resolve ``path`` and refuse anything except the active brief file.

    ``Path.resolve`` follows symlinks, so a link from ``active/brief.md`` into
    ``backups/`` fails here instead of being read.
    """
    allowed = active_brief_path(data_dir).resolve()
    resolved = path.resolve()
    hidden = HIDDEN_FROM_COPYTRADER.intersection(resolved.parts)
    if hidden or resolved != allowed:
        raise BriefIsolationError("copytrader paths cannot read reviewer archives")
    return resolved


def load_active_brief(data_dir: str | Path, *, chat_id: int | None = None) -> str:
    """Return the active channel playbook, or ``""`` when this channel has none yet.

    An empty string means triage keeps today's prompt. A missing file is that case,
    not an error: the reviewer may still be studying.
    """
    try:
        path = assert_copytrader_readable(active_brief_path(data_dir), data_dir)
    except BriefIsolationError:
        raise
    except OSError:
        return ""
    if not path.is_file():
        return ""
    try:
        marker = active_brief_path(data_dir).parent / IDENTITY_FILE
        # Resolve and confine the marker too; it is not allowed to point into an archive.
        if chat_id is not None:
            expected = marker.parent.resolve() / IDENTITY_FILE
            if marker.resolve() != expected:
                return ""
            before = json.loads(marker.read_text(encoding="utf-8"))
            if int(before.get("chat_id")) != int(chat_id) or not before.get("generation"):
                return ""
        content = path.read_text(encoding="utf-8")
        if chat_id is not None:
            after = json.loads(marker.read_text(encoding="utf-8"))
            if before != after:
                return ""
        return content
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return ""
