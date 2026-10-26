#!/usr/bin/env python3
"""Fail closed on private paths or recognizable credentials. Never print matched values."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

FORBIDDEN_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache',
                  'runtime-state', 'secrets', 'backups', 'deploy-runtime', 'migration-static',
                  '.release-work', 'data', '.wine', 'wineprefix'}
FORBIDDEN_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.session', '.pem', '.key', '.p12',
                      '.pfx', '.exe', '.dll', '.pyc', '.bak', '.zip', '.gz', '.tar', '.7z'}
PATTERNS = {
    'GitHub token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'Telegram bot token': re.compile(rb'\b[0-9]{6,15}:[A-Za-z0-9_-]{30,}\b'),
    'provider API key': re.compile(rb'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}\b'),
    'AWS access key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'credential URL': re.compile(rb'https?://[A-Za-z0-9_.-]+:[^\s/<>"\x27$}{]{8,}@'),
}


def path_allowed(relative: Path) -> bool:
    if any(part in FORBIDDEN_DIRS for part in relative.parts):
        return False
    name = relative.name.lower()
    return not (
        (name.startswith('.env') and name != '.env.example')
        or relative.suffix.lower() in FORBIDDEN_SUFFIXES
        or name in {'private-readme.md', 'accounts.dat', 'servers.dat', 'public_url.txt', 'tunnel_url'}
        or name.endswith(('.db-wal', '.db-shm', '.session-journal'))
    )


def scan_tree(root: Path) -> list[str]:
    errors = []
    for file in sorted(root.rglob('*')):
        relative = file.relative_to(root)
        if '.git' in relative.parts:
            continue
        if file.is_symlink():
            errors.append(f'{relative}: symlink refused')
            continue
        if not file.is_file():
            continue
        if not path_allowed(relative):
            errors.append(f'{relative}: private/generated path refused')
            continue
        if file.stat().st_size > 10 * 1024 * 1024:
            errors.append(f'{relative}: unexpected large public file')
            continue
        data = file.read_bytes()
        for label, pattern in PATTERNS.items():
            if pattern.search(data):
                errors.append(f'{relative}: {label} detected')
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    errors = scan_tree(args.directory.resolve())
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print('Public tree check passed (paths and credential patterns).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
