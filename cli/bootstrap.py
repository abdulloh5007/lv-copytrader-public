#!/usr/bin/env python3
"""Install a dependency-free lvct zipapp and a public source bundle. Never start Docker."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import stat
import sys
import tempfile
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipapp
import zipfile

REPOSITORY = 'abdulloh5007/lv-copytrader-public'
BLOCKED = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', 'runtime-state',
           'data', 'backups', 'secrets', '.wine', 'wineprefix', '.release-work', 'deploy-runtime', 'migration-static'}
WINDOWS_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10))}
SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.session', '.pem', '.key', '.p12', '.pfx', '.pyc', '.bak', '.exe', '.dll'}
# Offline/developer installs copy product paths, never the whole private checkout.
PRODUCT_PATHS = ('VERSION', 'README.md', 'CHANGELOG.md', 'LICENSE', 'NOTICE', '.env.example', '.dockerignore',
                 'docker-compose.yml', 'compose.arm64.yml', 'cli', 'install.sh', 'install.ps1',
                 'backend/app', 'backend/pyproject.toml', 'backend/requirements.lock', 'backend/Dockerfile',
                 'frontend/src', 'frontend/public', 'frontend/tests', 'frontend/package.json',
                 'frontend/package-lock.json', 'frontend/index.html', 'frontend/tsconfig.json',
                 'frontend/tsconfig.node.json', 'frontend/vite.config.ts', 'mt5-sidecar', 'deploy/arm64', 'docs/CLI.md', 'docs/CLI-AGENTS.md', 'docs/SUPPORT.md', 'docs/README.md',
                 'docs/ARCHITECTURE.md', 'docs/CONTRACTS.md', 'docs/BUILD_PROMPT.md', 'docs/CHANNEL_BRIEF.md',
                 'docs/DEMO.md', 'docs/SECURITY-AUDIT.md', 'SECURITY.md', 'RUNBOOK.md')


def allowed(path: PurePosixPath) -> bool:
    return bool(path.parts) and not path.is_absolute() and not any(p in ('..', '') or p in BLOCKED or p.split('.')[0].upper() in WINDOWS_RESERVED or p.endswith((' ', '.')) or any(ord(c) < 32 for c in p) for p in path.parts) and not any(
        p.startswith('.env') and p != '.env.example' for p in path.parts) and path.suffix.lower() not in SUFFIXES and path.name.lower() not in ('accounts.dat', 'servers.dat')


def fetch(url: str, target: Path | None = None, limit: int = 256 * 1024 * 1024) -> bytes | None:
    request = Request(url, headers={'User-Agent': 'lvct-installer', 'Accept': 'application/vnd.github+json'})
    with urlopen(request, timeout=60) as response:
        output = target.open('wb') if target else None
        blocks, total = [], 0
        try:
            while True:
                block = response.read(128 * 1024)
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise ValueError('Download is unexpectedly large')
                if output:
                    output.write(block)
                else:
                    blocks.append(block)
        finally:
            if output:
                output.close()
        return None if target else b''.join(blocks)


def unpack(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as package:
        if len(package.infolist()) > 30_000 or sum(x.file_size for x in package.infolist()) > 512 * 1024 * 1024:
            raise ValueError('Source archive is too large')
        roots = {PurePosixPath(x.filename).parts[0] for x in package.infolist() if x.filename}
        if len(roots) != 1:
            raise ValueError('Expected one source archive root')
        seen = set()
        for entry in package.infolist():
            parts = PurePosixPath(entry.filename).parts
            if '\\' in entry.filename or ':' in entry.filename or entry.filename.startswith('/') or '..' in parts:
                raise ValueError('Unsafe archive path')
            relative = PurePosixPath(*parts[1:])
            if len(parts) == 1:
                continue
            if not allowed(relative) or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('Private or symlinked path refused')
            if entry.is_dir():
                continue
            key = str(relative).casefold()
            if key in seen:
                raise ValueError('Duplicate archive path')
            seen.add(key)
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(entry) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)
            if entry.external_attr >> 16 & 0o111:
                target.chmod(0o755)


def local_source(source: Path, destination: Path) -> str:
    digest = hashlib.sha256()
    files: set[Path] = set()
    for name in PRODUCT_PATHS:
        path = source / name
        if path.is_symlink():
            raise ValueError('Symlinked product path refused')
        if path.is_dir():
            files.update(path.rglob('*'))
        elif path.is_file():
            files.add(path)
    for file in sorted(files):
        relative = file.relative_to(source)
        if not allowed(PurePosixPath(relative.as_posix())):
            continue
        if file.is_symlink():
            raise ValueError('Symlinked product file refused')
        if not file.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target)
        digest.update(relative.as_posix().encode() + b'\0' + file.read_bytes())
    return 'local-' + digest.hexdigest()[:20]


def default_home() -> Path:
    if os.environ.get('LVCT_HOME'):
        return Path(os.environ['LVCT_HOME']).expanduser().absolute()
    if platform.system() == 'Windows':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'lvct'
    if platform.system() == 'Darwin':
        return Path.home() / 'Library/Application Support/lvct'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'lvct'


def atomic(path: Path, text: str, mode: int = 0o600) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Symlinked installer destination refused')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(args) -> None:
    home = args.home.expanduser().absolute() if args.home else default_home()
    if home.is_symlink():
        raise ValueError('Symlinked installation directory refused')
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='lvct-install-') as temporary:
        work = Path(temporary)
        bundle = work / 'source'
        bundle.mkdir()
        if args.source:
            revision = local_source(args.source.resolve(), bundle)
        elif getattr(args,'distribution',None):
            if args.distribution != 'https://docs.lvenc.site/releases/current.json':
                raise ValueError('Only the official HTTPS distribution manifest is accepted')
            manifest=json.loads(fetch(args.distribution,limit=16*1024))
            digest=manifest.get('sha256','')
            if (manifest.get('schema')!=1 or not re.fullmatch(r'[0-9a-f]{64}',digest)
                or type(manifest.get('size')) is not int or not 0<manifest['size']<=256*1024*1024
                or manifest.get('url')!='https://docs.lvenc.site/releases/'+digest+'.zip'):
                raise ValueError('Invalid official distribution manifest')
            archive=work/'source.zip'
            fetch(manifest['url'],archive)
            if archive.stat().st_size!=manifest['size'] or hashlib.sha256(archive.read_bytes()).hexdigest()!=digest:
                raise ValueError('Distribution archive integrity mismatch')
            revision='download-'+digest
            print('Downloading official '+str(manifest.get('channel','preview'))+' build '+digest[:12]+' …',flush=True)
            unpack(archive,bundle)
        else:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,199}', args.ref):
                raise ValueError('Invalid source revision')
            metadata = json.loads(fetch('https://api.github.com/repos/' + REPOSITORY + '/commits/' + quote(args.ref, safe=''), limit=2 * 1024 * 1024))
            revision = metadata.get('sha', '')
            if not re.fullmatch(r'[0-9a-f]{40}', revision):
                raise ValueError('GitHub did not return an immutable commit')
            print('Downloading public source commit ' + revision + ' …', flush=True)
            archive = work / 'source.zip'
            fetch('https://codeload.github.com/' + REPOSITORY + '/zip/' + revision, archive)
            unpack(archive, bundle)
        required = ['VERSION', 'docker-compose.yml', '.env.example', 'cli/lvct/cli.py']
        if not all((bundle / p).is_file() for p in required):
            raise ValueError('This revision does not contain the CLI and runtime source')
        target = home / 'releases' / revision
        # Immutable source directories are never replaced beneath a running installation.
        if target.is_symlink() or target.parent.is_symlink():
            raise ValueError('Symlinked release directory refused')
        if target.exists():
            for original in bundle.rglob('*'):
                if not original.is_file():
                    continue
                relative = original.relative_to(bundle)
                cached = target / relative
                if cached.is_symlink() or any((target / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts))):
                    raise ValueError('Symlinked cached source refused')
                if not cached.is_file() or hashlib.sha256(cached.read_bytes()).digest() != hashlib.sha256(original.read_bytes()).digest():
                    raise ValueError('Cached source differs from the selected immutable revision')
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            staged = Path(tempfile.mkdtemp(prefix='.lvct-source-', dir=target.parent))
            try:
                shutil.copytree(bundle, staged, dirs_exist_ok=True)
                os.replace(staged, target)
            finally:
                if staged.exists():
                    shutil.rmtree(staged)
        stage = work / 'application'
        shutil.copytree(bundle / 'cli/lvct', stage / 'lvct')
        (stage / '__main__.py').write_text('import os\nos.environ.setdefault("LVCT_HOME", ' + repr(str(home)) + ')\nfrom lvct.cli import main\nraise SystemExit(main())\n', encoding='utf-8')
        executable = home / 'bin/lvct.pyz'
        if executable.is_symlink():
            raise ValueError('Symlinked executable refused')
        executable.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        archive = work / 'lvct.pyz'
        zipapp.create_archive(stage, archive, interpreter=sys.executable, compressed=True)
        # Copy to the same filesystem before atomic replacement.
        fd, temporary_executable = tempfile.mkstemp(dir=executable.parent)
        os.close(fd)
        try:
            shutil.copyfile(archive, temporary_executable)
            os.chmod(temporary_executable, 0o700)
            os.replace(temporary_executable, executable)
        finally:
            if os.path.exists(temporary_executable):
                os.unlink(temporary_executable)
        atomic(home / 'bootstrap.json', json.dumps({'source': str(target), 'revision': revision}, indent=2))
        if not args.no_path:
            if platform.system() == 'Windows':
                command = home / 'bin/lvct.cmd'
                if command.exists() and 'LVCT managed launcher' not in command.read_text(encoding='utf-8'):
                    raise ValueError('An unrelated lvct command already exists')
                atomic(command, '@rem LVCT managed launcher\n@chcp 65001 >nul\n@"' + sys.executable.replace('%', '%%') + '" "%~dp0lvct.pyz" %*\n', 0o700)
                import winreg
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
                    try:
                        old, kind = winreg.QueryValueEx(key, 'Path')
                    except FileNotFoundError:
                        old, kind = '', winreg.REG_EXPAND_SZ
                    if str(command.parent).casefold() not in [p.casefold() for p in old.split(';')]:
                        winreg.SetValueEx(key, 'Path', 0, kind, old.rstrip(';') + ';' + str(command.parent))
                print('Open a new terminal for the updated user PATH, or use: ' + str(command))
            else:
                command = (args.bin_dir or Path.home() / '.local/bin') / 'lvct'
                if command.is_symlink() or (command.exists() and 'LVCT managed launcher' not in command.read_text(encoding='utf-8')):
                    raise ValueError('An unrelated lvct command already exists')
                atomic(command, '#!/bin/sh\n# LVCT managed launcher\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(executable)) + ' "$@"\n', 0o700)
                print('Command: ' + str(command))
                if str(command.parent) not in os.environ.get('PATH', '').split(os.pathsep):
                    print('Add to your shell PATH: export PATH=' + shlex.quote(str(command.parent)) + ':"$PATH"')
    print('Installed. No containers were started and existing runtime configuration was preserved.')
    print('Next: lvct --help, lvct deps, lvct init')


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty() and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, help='Offline install from explicitly selected product paths')
    p.add_argument('--distribution',help='Official LVCT HTTPS distribution manifest')
    p.add_argument('--ref', default='main', help='Public branch/tag/commit; resolved to a commit before download')
    p.add_argument('--home', type=Path)
    p.add_argument('--bin-dir', type=Path)
    p.add_argument('--no-path', action='store_true', help='Do not create a launcher or edit user PATH')
    p.add_argument('--install-python', action='store_true', help=argparse.SUPPRESS)
    args = p.parse_args()
    try:
        install(args)
    except (OSError, ValueError, zipfile.BadZipFile):
        print('Installation failed; check paths, GitHub access and the selected public revision. No secrets were printed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
