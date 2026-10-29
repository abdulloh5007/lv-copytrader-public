"""Bounded, private CLI failure journal; no payloads, stack locals or automatic uploads."""
from datetime import datetime, timezone
import json
from . import config
from .privacy import scrub


def record(command: str, message: str, *, critical=False):
    try:
        path=config.home()/'logs/cli.jsonl'
        if path.is_symlink():return
        previous=path.read_text(encoding='utf-8')[-128_000:] if path.exists() else ''
        lines=previous.splitlines()[-199:]
        try:values=config.read_env()
        except Exception:values={}
        lines.append(json.dumps({'time':datetime.now(timezone.utc).isoformat(),'level':'CRITICAL' if critical else 'ERROR',
                                 'command':command,'message':scrub(message,values)},ensure_ascii=False))
        config.secure_write(path,'\n'.join(lines)+'\n')
    except Exception:
        pass  # Diagnostic storage must never hide the original failure.


def recent():
    path=config.home()/'logs/cli.jsonl'
    if not path.exists() or path.is_symlink():return []
    if path.stat().st_size>512_000:return []
    try:return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()[-50:]]
    except ValueError:return []
