"""Reviewer-only storage: the active brief, its study notes, and channel-change backups.

Signal workers do not import this module. Backups are written here and never passed
back through :func:`app.reviewer.brief.load_active_brief`.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.reviewer.layout import (
    ACTIVE_DIR,
    BACKUP_DIR,
    BRIEF_FILE,
    CURRENT_DIR,
    META_FILE,
    REVIEWER_DIR,
    ROOT_DIR,
    STUDY_FILE,
)


def _root(data_dir: str | Path) -> Path:
    return Path(data_dir) / ROOT_DIR


def _active_brief(data_dir: str | Path) -> Path:
    return _root(data_dir) / ACTIVE_DIR / BRIEF_FILE


def _current_dir(data_dir: str | Path) -> Path:
    return _root(data_dir) / REVIEWER_DIR / CURRENT_DIR


def _backup_root(data_dir: str | Path) -> Path:
    return _root(data_dir) / BACKUP_DIR


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def read_meta(data_dir: str | Path) -> dict[str, Any] | None:
    """Return the current study's meta, or None when this channel has not been touched."""
    return _read_json(_current_dir(data_dir) / META_FILE)


def brief_ready_for(data_dir: str | Path, chat_id: int) -> bool:
    """True when the active brief was finished for this exact chat."""
    meta = read_meta(data_dir)
    if not meta or str(meta.get("status") or "") != "ready":
        return False
    try:
        recorded = int(meta.get("chat_id"))
    except (TypeError, ValueError):
        return False
    if recorded != int(chat_id):
        return False
    path = _active_brief(data_dir)
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def attempts_for(data_dir: str | Path, chat_id: int) -> int:
    """How many failed studies this chat already has. A different chat counts as zero."""
    meta = read_meta(data_dir) or {}
    try:
        recorded = int(meta.get("chat_id"))
    except (TypeError, ValueError):
        return 0
    if recorded != int(chat_id):
        return 0
    try:
        return max(0, int(meta.get("attempts") or 0))
    except (TypeError, ValueError):
        return 0


def clip_lines(text: str, max_lines: int) -> str:
    """Keep at most ``max_lines`` non-empty-trailing lines."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if not lines:
        return ""
    return "\n".join(lines).rstrip() + "\n"


def write_meta(data_dir: str | Path, meta: dict[str, Any]) -> None:
    """Replace the current study meta. Does not touch the active brief."""
    _atomic_write(
        _current_dir(data_dir) / META_FILE,
        json.dumps(meta, ensure_ascii=False, indent=2, default=_json_default),
    )


def write_ready(
    data_dir: str | Path,
    *,
    brief: str,
    meta: dict[str, Any],
    study: dict[str, Any],
    max_lines: int,
) -> str:
    """Publish a finished brief, then the study notes, then meta last.

    Meta is the readiness flag. Writing it last means a reader never sees
    ``status=ready`` against a brief that has not landed yet.
    """
    clipped = clip_lines(brief, max_lines)
    _atomic_write(_active_brief(data_dir), clipped)
    _atomic_write(
        _current_dir(data_dir) / STUDY_FILE,
        json.dumps(study, ensure_ascii=False, indent=2, default=_json_default),
    )
    stored = dict(meta)
    stored["lines"] = clipped.count("\n")
    stored["status"] = "ready"
    write_meta(data_dir, stored)
    return clipped


def backup_current(data_dir: str | Path) -> Path | None:
    """Move the active brief and the current study notes into ``backups/``.

    Returns the backup directory, or None when there was nothing to keep. The
    active slot is empty afterwards so the next channel cannot inherit this one.
    """
    brief = _active_brief(data_dir)
    current = _current_dir(data_dir)
    meta_path = current / META_FILE
    study_path = current / STUDY_FILE
    have_brief = brief.is_file()
    have_meta = meta_path.is_file()
    have_study = study_path.is_file()
    if not (have_brief or have_meta or have_study):
        return None

    meta = _read_json(meta_path) or {}
    try:
        chat_id = int(meta.get("chat_id"))
    except (TypeError, ValueError):
        chat_id = 0
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    destination = _backup_root(data_dir) / f"c{chat_id}_{stamp}"
    suffix = 0
    while destination.exists():
        suffix += 1
        destination = _backup_root(data_dir) / f"c{chat_id}_{stamp}_{suffix}"
    destination.mkdir(parents=True, exist_ok=False)
    if have_brief:
        os.replace(brief, destination / BRIEF_FILE)
    if have_meta:
        os.replace(meta_path, destination / META_FILE)
    if have_study:
        os.replace(study_path, destination / STUDY_FILE)
    return destination


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
