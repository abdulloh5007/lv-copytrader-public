"""Select an uncommon loopback port on the Docker host; never reserve it indefinitely."""
from __future__ import annotations
import json
import shlex
import socket

from . import config, runtime
from .console import Error

DEFAULT_PORT = 19471
PORT_ATTEMPTS = 100

REMOTE_PROBE = '''import json,socket,sys
start,count=map(int,sys.argv[1:])
for port in range(start,min(start+count,65536)):
 s=socket.socket()
 try:s.bind(('127.0.0.1',port))
 except OSError:s.close();continue
 s.close();print(json.dumps({'port':port}));break
else:raise SystemExit(2)
'''


def local_port(preferred: int, count: int = PORT_ATTEMPTS) -> int:
    for port in range(preferred, min(preferred + count, 65536)):
        with socket.socket() as probe:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise Error('No free panel port in the selected range. Choose --port PORT.')


def select(preferred: int | None, endpoint: str | None = None) -> int:
    port = DEFAULT_PORT if preferred is None else preferred
    if not 1024 <= port <= 65535:
        raise Error('Choose an unprivileged port between 1024 and 65535.')
    count = PORT_ATTEMPTS if preferred is None else 1
    if endpoint and endpoint.startswith('ssh://'):
        user, host, ssh_port = config.ssh_target(endpoint)
        result = runtime.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-p', str(ssh_port),
                              '--', f'{user}@{host}', shlex.join(['python3', '-c', REMOTE_PROBE, str(port), str(count)])], timeout=20)
        try:
            chosen = json.loads(result.stdout)['port']
            if not isinstance(chosen, int) or not port <= chosen < min(port + count, 65536):
                raise ValueError()
            return chosen
        except (ValueError, KeyError, TypeError) as e:
            raise Error('The remote port probe failed. The SSH host needs Python 3.') from e
    return local_port(port, count)
