"""Owner-only CLI state. Runtime configuration never writes the source checkout's .env."""
from __future__ import annotations
import json
import os
from pathlib import Path
import platform
import re
import tempfile
from urllib.parse import urlsplit

from .console import Error


def home() -> Path:
    if os.environ.get('LVCT_HOME'):
        return Path(os.environ['LVCT_HOME']).expanduser().absolute()
    if platform.system() == 'Windows':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'lvct'
    if platform.system() == 'Darwin':
        return Path.home() / 'Library/Application Support/lvct'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'lvct'


def secure_write(path: Path, text: str) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise Error('Refusing symlinked configuration paths.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.lvct-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict:
    if path.is_symlink():
        raise Error('Refusing a symlinked state file.')
    if path.stat().st_size > 64 * 1024:
        raise Error('State file is too large.')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as e:
        raise Error('Invalid state file; it was not reset.') from e
    if not isinstance(value, dict):
        raise Error('Invalid state file.')
    return value


def load() -> dict:
    path = home() / 'installation.json'
    if not path.exists():
        raise Error('No installation yet. Run lvct init.')
    value = read_json(path)
    ident = value.get('id', '')
    if not re.fullmatch(r'[a-f0-9]{12}', ident) or value.get('project') != 'lvct-' + ident:
        raise Error('Invalid installation identity; refusing Docker operations.')
    if not isinstance(value.get('source'), str) or not (Path(value['source']) / 'docker-compose.yml').is_file():
        raise Error('Source bundle is missing. Restore it before operating this installation.')
    context_name(value.get('context', ''))
    if not isinstance(value.get('port'), int) or not 1024 <= value['port'] <= 65535:
        raise Error('Invalid dashboard port.')
    return value


def source_root(explicit: str | None = None) -> Path:
    if explicit:
        result = Path(explicit).expanduser().resolve()
    elif (home() / 'bootstrap.json').exists():
        result = Path(read_json(home() / 'bootstrap.json')['source'])
    else:
        result = Path(__file__).resolve().parents[2]
    if not all((result / name).is_file() for name in ('docker-compose.yml', '.env.example', 'VERSION')):
        raise Error('No source bundle found. Use the installer or init --source PATH.')
    return result


def context_name(name: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', name):
        raise Error('Invalid Docker context name.')
    return name


def ssh_target(value: str) -> tuple[str, str, int]:
    raw = value if value.startswith('ssh://') else 'ssh://' + value
    try:
        url = urlsplit(raw)
        port = url.port or 22
    except ValueError as e:
        raise Error('Use an SSH target such as user@server.example or ssh://user@server:22.') from e
    if (url.scheme != 'ssh' or url.password or url.query or url.fragment or url.path not in ('', '/')
        or not url.hostname or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*', url.hostname)
        or not url.username or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', url.username)
        or not 1 <= port <= 65535):
        raise Error('Use SSH key authentication and an explicit user@host; no password in URLs.')
    return url.username, url.hostname, port


def env_path() -> Path:
    return home() / 'runtime.env'


def write_env(values: dict[str, str]) -> None:
    # Compose interpolates dollars in double-quoted dotenv values; $$ is a literal dollar.
    lines = ['# Managed by lvct. Private; never publish this file.']
    for key, value in values.items():
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or any(ord(c) < 32 for c in str(value)):
            raise Error('Invalid environment value.')
        lines.append(key + '=' + json.dumps(str(value).replace('$', '$$'), ensure_ascii=False))
    secure_write(env_path(), '\n'.join(lines) + '\n')


def read_env() -> dict[str, str]:
    path = env_path()
    if path.is_symlink() or path.stat().st_size > 64 * 1024:
        raise Error('Invalid environment file.')
    values = {}
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            if not line or line.startswith('#'):
                continue
            key, raw = line.split('=', 1)
            value = json.loads(raw)
            if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or not isinstance(value, str):
                raise ValueError('Invalid value')
            values[key] = value.replace('$$', '$')
    except (ValueError, OSError) as e:
        raise Error('Cannot read managed environment file; no values were printed.') from e
    return values


def redact(text: str, values: dict[str, str] | None = None) -> str:
    for key, value in sorted((values or {}).items(), key=lambda item: len(item[1]), reverse=True):
        if value and (re.search(r'PASSWORD|TOKEN|KEY|HASH|SESSION|PHONE', key) or key == 'MT5_LOGIN'):
            variants = {value, json.dumps(value, ensure_ascii=False)[1:-1],
                        value.replace('$', '$$'), json.dumps(value.replace('$', '$$'), ensure_ascii=False)[1:-1]}
            for variant in sorted(variants, key=len, reverse=True):
                text = text.replace(variant, '[hidden]')
    text = re.sub(r'(?i)(authorization\s*[:=]\s*)(?:basic|bearer)\s+[^\s]+', r'\1[hidden]', text)
    return text
