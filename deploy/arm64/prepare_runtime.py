#!/usr/bin/env python3
"""Prepare the tested FEX/Wine runtime; never reads credentials or starts trading."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import uuid

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PIN = json.loads((HERE / 'runtime.json').read_text())


def run(*command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise RuntimeError('Command failed: ' + command[0] + '\n' + result.stderr[-1500:])
    return result.stdout


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_file(source, target, mode):
    if target.is_symlink():
        raise RuntimeError('Refusing symlinked target: ' + str(target))
    if target.exists():
        if target.read_bytes() != source.read_bytes():
            raise RuntimeError('Existing runtime differs; use a separate --root: ' + str(target))
        return
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(mode)


def files(root):
    return [(HERE / 'entrypoint', root / 'runtime/arm64-entrypoint', 0o755),
            (REPO / 'mt5-sidecar/rpc_server.py', root / 'runtime/rpc_server.py', 0o644),
            *[(HERE / name, root / 'fex' / name, 0o755) for name in ['wine', 'wineserver', 'grep']]]


def validate(root, image):
    for name, expected in PIN['binary_sha256'].items():
        require(digest(root / 'fex/extracted/usr/bin' / name) == expected, name + ' checksum differs')
    for source, target, _ in files(root):
        require(source.read_bytes() == target.read_bytes(), 'Runtime differs: ' + str(target))
    elf = (root / 'runtime/wineserver.real').read_bytes()[:20]
    require(elf[:5] == b'\x7fELF\x02' and elf[18:20] == b'\x3e\x00', 'Expected x86-64 wineserver')
    info = json.loads(run('docker', 'image', 'inspect', image))[0]
    require(info['Architecture'] == 'amd64', 'Expected amd64 image')
    version = run('docker', 'run', '--rm', '--network', 'none', '--platform', 'linux/amd64',
                  '--entrypoint', '/opt/wine-stable/bin/wine', image, '--version').strip()
    require(version == 'wine-' + PIN['wine_version'], 'Untested Wine version: ' + version)
    return {'validated': True, 'wine': version, 'fex': PIN['version'], 'image_id': info['Id']}


def install(root, image):
    if root.is_symlink():
        raise RuntimeError('Refusing symlinked runtime root')
    # Preflight every existing wrapper before changing files used by a running container.
    for source, target, _ in files(root):
        if target.exists() and (target.is_symlink() or source.read_bytes() != target.read_bytes()):
            raise RuntimeError('Existing runtime differs; use a separate --root: ' + str(target))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='copytrader-fex-') as temporary:
        stage = Path(temporary)
        destination = root / 'fex/extracted'
        if not destination.exists():
            run('add-apt-repository', '-y', '--no-update', 'ppa:fex-emu/fex')
            run('apt-get', 'update')
            result = subprocess.run(['apt-get', 'download', PIN['package'] + '=' + PIN['version']],
                                    cwd=stage, capture_output=True, text=True, timeout=300)
            if result.returncode:
                raise RuntimeError('Pinned FEX package unavailable; do not substitute an untested version')
            package = next(stage.glob('*.deb'))
            require(digest(package) == PIN['package_sha256'], 'FEX package checksum mismatch')
            run('dpkg-deb', '-x', str(package), str(stage / 'extracted'))
            for name, expected in PIN['binary_sha256'].items():
                require(digest(stage / 'extracted/usr/bin' / name) == expected, name + ' checksum differs')
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copytree(stage / 'extracted', destination)
        else:
            for name, expected in PIN['binary_sha256'].items():
                require(digest(destination / 'usr/bin' / name) == expected, 'Existing FEX differs')
        container = 'copytrader-wineserver-extract-' + uuid.uuid4().hex
        try:
            run('docker', 'create', '--name', container, '--network', 'none', '--platform', 'linux/amd64',
                '--entrypoint', '/bin/true', image)
            run('docker', 'cp', container + ':/opt/wine-stable/bin/wineserver', str(stage / 'wineserver.real'))
            install_file(stage / 'wineserver.real', root / 'runtime/wineserver.real', 0o755)
        finally:
            subprocess.run(['docker', 'rm', container], capture_output=True)
        for source, target, mode in files(root):
            install_file(source, target, mode)
    return validate(root, image)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true', help='Read-only validation; no credentials or broker calls')
    mode.add_argument('--install', action='store_true', help='Prepare files; MT5 and trading remain stopped')
    parser.add_argument('--root', type=Path, default=Path('/opt/copytrader-compat'))
    parser.add_argument('--image', default='signalpilot/mt5-sidecar:local')
    args = parser.parse_args()
    if platform.machine() != 'aarch64' or os.geteuid() != 0:
        parser.error('Run with sudo on the ARM64 Ubuntu host')
    root = args.root.absolute()
    if args.install:
        os_release = Path('/etc/os-release').read_text()
        if 'ID=ubuntu\n' not in os_release or 'VERSION_ID="24.04"' not in os_release:
            parser.error('Pinned runtime was verified on Ubuntu 24.04 only')
    result = install(root, args.image) if args.install else validate(root, args.image)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
