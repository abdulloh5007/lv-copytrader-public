"""Explicit Docker contexts and unique volumes; never a broker test order."""
from __future__ import annotations
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

from . import config
from .console import Error, clean, say


def child_env(values: dict[str, str] | None = None) -> dict[str, str]:
    result = dict(os.environ)
    for key in list(result):
        if key.startswith(('COMPOSE_', 'SP_', 'MT5_', 'TELEGRAM_', 'SIDECAR_', 'LVCT_')) or key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH') or key in (values or {}):
            result.pop(key, None)
    return result


def run(args: list[str], *, values: dict[str, str] | None = None, data: str | None = None,
        check: bool = True, timeout: int | None = 60) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, input=data, text=True, encoding='utf-8', errors='replace',
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env(values), timeout=timeout)
    except FileNotFoundError as e:
        raise Error(f'{Path(args[0]).name} is missing. Run lvct deps for installation options.') from e
    except subprocess.TimeoutExpired as e:
        raise Error('Command timed out. Check Docker/SSH access; no retry was sent.') from e
    if check and result.returncode:
        detail = clean(config.redact(result.stderr or result.stdout, values))[-2400:]
        raise Error(detail or 'Command failed; check Docker and SSH access.')
    return result


def docker(state: dict, *args: str) -> list[str]:
    return ['docker', '--context', config.context_name(state['context']), *args]


def compose(state: dict, *args: str) -> list[str]:
    return docker(state, 'compose', '--project-name', state['project'], '--env-file', str(config.env_path()),
                  '-f', str(Path(state['source']) / 'docker-compose.yml'), *args)


def checked_env(state: dict) -> dict[str, str]:
    values = config.read_env()
    project = state['project']
    expected = {'LVCT_ENV_FILE': str(config.env_path()), 'LVCT_PORT': str(state['port']),
                'LVCT_APP_IMAGE': project + '-app:local', 'LVCT_SIDECAR_IMAGE': project + '-mt5:local',
                'LVCT_DATA_VOLUME': project + '_data', 'LVCT_MT5_VOLUME': project + '_mt5',
                'LVCT_X11_VOLUME': project + '_x11', 'SP_ALLOW_UNAUTHENTICATED': 'false'}
    if any(values.get(key) != value for key, value in expected.items()):
        raise Error('Managed deployment identity was changed in runtime.env; refusing to target different resources.')
    return values


def execute(state: dict, *args: str, **kwargs) -> subprocess.CompletedProcess:
    values = checked_env(state)
    ensure_endpoint(state)
    return run(compose(state, *args), values=values, **kwargs)


def info(state: dict) -> dict:
    result = run(docker(state, 'info', '--format', '{{json .}}'))
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except ValueError as e:
        raise Error('Docker returned an invalid engine description.') from e


def context_host(state: dict) -> str:
    result = run(docker(state, 'context', 'inspect', state['context']))
    try:
        endpoint = json.loads(result.stdout)[0]['Endpoints']['docker']['Host']
        if not isinstance(endpoint, str):
            raise ValueError('Invalid endpoint')
        return endpoint
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise Error('Cannot determine the Docker context endpoint.') from e


def ensure_endpoint(state: dict) -> str:
    endpoint = context_host(state)
    if endpoint.startswith('ssh://'):
        config.ssh_target(endpoint)
    elif not endpoint.startswith(('unix://', 'npipe://')):
        raise Error('Use a local Docker socket or SSH context. TCP endpoints are not used for private configuration.')
    return endpoint


def check_engine(state: dict, *, display: bool = True) -> dict:
    ensure_endpoint(state)
    data = info(state)
    if data.get('OSType') != 'linux':
        raise Error('Switch Docker Desktop to Linux containers, then retry lvct doctor.')
    if data.get('Architecture') not in ('x86_64', 'amd64'):
        raise Error('Managed local MT5 currently requires a Linux amd64 engine. Apple Silicon local execution is not implemented/validated by this CLI. Remote mode uses an existing Linux x64 computer/VPS you provide: init --remote user@HOST. Existing Linux ARM/FEX installs use deploy/arm64/README.md.')
    version = run(docker(state, 'compose', 'version', '--short')).stdout
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (2, 24, 0):
        raise Error('Docker Compose 2.24+ is required. Run lvct deps.')
    if display:
        say(f"  OK  Linux {data['Architecture']} · Docker {data.get('ServerVersion', '?')} · Compose {version.strip()}", '32')
        memory = data.get('MemTotal', 0)
        if memory and memory < 4 * 1024**3:
            say('  NOTE  Less than 4 GiB allocated to Docker; Wine/MT5 builds may need more memory.', '33')
    return data


def services(state: dict) -> list[dict]:
    result = execute(state, 'ps', '--all', '--format', 'json')
    text = result.stdout.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
        return value if isinstance(value, list) else [value]
    except ValueError:
        try:
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError as e:
            raise Error('Docker returned an invalid service list.') from e


def volume_names(state: dict) -> list[str]:
    return [state['project'] + suffix for suffix in ('_data', '_mt5', '_x11')]


def prepare_volumes(state: dict) -> None:
    for name in volume_names(state):
        found = run(docker(state, 'volume', 'inspect', name), check=False)
        if found.returncode:
            if 'no such volume' not in found.stderr.lower():
                raise Error('Cannot inspect the installation volumes; refusing to create replacements.')
            run(docker(state, 'volume', 'create', '--label', 'lvct.installation=' + state['id'], name))
        else:
            try:
                owned = json.loads(found.stdout)[0].get('Labels', {}).get('lvct.installation')
            except (ValueError, TypeError, IndexError, AttributeError):
                owned = None
            if owned != state['id']:
                raise Error('Volume name collision: the existing volume is not owned by this installation.')


def stream(state: dict, args: list[str], transform=None) -> int:
    values = checked_env(state)
    ensure_endpoint(state)
    try:
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              encoding='utf-8', errors='replace', env=child_env(values)) as process:
            try:
                for line in process.stdout:
                    output = config.redact(line.rstrip('\n'), values)
                    if transform:
                        output = transform(output)
                    if output is not None:
                        say(output)
                return process.wait()
            except KeyboardInterrupt:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                return 130
    except FileNotFoundError as e:
        raise Error('Docker is missing; run lvct deps.') from e


def dependency_plan() -> tuple[list[str] | None, str]:
    system = platform.system()
    if system == 'Windows':
        return (['winget', 'install', '--exact', '--id', 'Docker.DockerDesktop'], 'Docker Desktop (Linux containers / WSL 2). Start Docker Desktop after installation.')
    if system == 'Darwin':
        return (['brew', 'install', '--cask', 'docker-desktop'] if shutil.which('brew') else None,
                'Docker Desktop: https://docs.docker.com/desktop/setup/install/mac-install/ . Start the app and complete its first-run setup.')
    return (None, 'Docker Engine + Compose: https://docs.docker.com/engine/install/ . deps --install can install Docker from its official convenience script on a fresh Linux host.')
