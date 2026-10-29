"""The lvct command line. Operations always target one explicitly isolated installation."""
from __future__ import annotations
import argparse
from contextlib import nullcontext
import base64
import json
import os
import platform
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
import webbrowser

from . import __version__, config, runtime, diagnostics, ports, updates, events
from .console import Error, ask, banner, confirm, paint, password, say, validate_password


class ConsoleParser(argparse.ArgumentParser):
    def format_help(self) -> str:
        return paint('LV / COPYTRADER  ·  lvct ' + __version__, '1;36') + '\n\n' + super().format_help()


def parser() -> argparse.ArgumentParser:
    p = ConsoleParser(prog='lvct', description='LV Copytrader — setup, run and manage your private installation.',
                                epilog='First run: lvct deps → lvct init → lvct doctor → lvct start. Trading is armed only in the dashboard.')
    p.add_argument('--version', action='version', version='lvct ' + __version__)
    p.add_argument('--home', type=Path, help='Use a separate installation directory')
    p.add_argument('--no-color', action='store_true', help='Disable terminal colours')
    sub = p.add_subparsers(dest='command')
    init = sub.add_parser('init', aliases=['setup'], help='Create a private configuration using a guided wizard')
    init.add_argument('--source', help='Use this source checkout rather than the installed bundle')
    init.add_argument('--context', help='Use an existing Docker context (never changes your global default)')
    init.add_argument('--remote', help='Use a Linux x64 computer/VPS you supply over SSH: user@host')
    init.add_argument('--port', type=int, help='Loopback dashboard port (default: first free from 19471)')
    init.add_argument('--from-stdin', action='store_true', help='Read a JSON setup object from stdin; secrets never belong in flags')
    doctor = sub.add_parser('doctor', help='Check Docker, Compose, engine architecture and configuration without starting services')
    doctor.add_argument('--context', help='Check a Docker context before initial setup')
    doctor.add_argument('--json', action='store_true', help='Print a small machine-readable engine report')
    health = sub.add_parser('health', help='Check app, MT5, Telegram and provider readiness without trading')
    health.add_argument('--json', action='store_true')
    health.add_argument('--watch', action='store_true', help='Stay in foreground until Ctrl+C')
    health.add_argument('--interval', type=int, default=10, help='Watch interval, 5..300 seconds')
    errors = sub.add_parser('errors', help='Group recent errors by likely cause with next steps')
    errors.add_argument('--json', action='store_true')
    errors.add_argument('--tail', type=int, default=300)
    errors.add_argument('--since', default='1h')
    errors.add_argument('--service', choices=['app','mt5-sidecar'])
    update = sub.add_parser('update', help='Check public updates; apply/rollback only with stopped services')
    mode = update.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Read-only check (default)')
    mode.add_argument('--apply', action='store_true', help='Build and prepare stopped services; never starts trading')
    mode.add_argument('--rollback', action='store_true', help='Restore previous images/source; services stay stopped')
    update.add_argument('--ref', default='main', help='Official public branch, tag or commit')
    update.add_argument('--source', type=Path, help='Apply reviewed offline source instead of downloading')
    update.add_argument('--json', action='store_true', help='Machine-readable --check result')
    report = sub.add_parser('report', help='Save a minimised support report locally; --send explicitly uploads it')
    report.add_argument('--send', action='store_true', help='Upload a saved report to official support')
    report.add_argument('--file', type=Path, help='Previously saved report to send after review')
    report.add_argument('--note', default='', help='Short issue description; do not include credentials')
    report.add_argument('--contact', help='Optional reply email; stored only in the submitted report')
    report.add_argument('--yes', action='store_true', help='Confirm upload of the reviewed report')
    deps = sub.add_parser('deps', help='Show prerequisites and optional official dependency installation')
    deps.add_argument('--install', action='store_true', help='Install missing Docker using the OS installer')
    deps.add_argument('--yes', action='store_true', help='Confirm the displayed dependency installation plan')
    start = sub.add_parser('start', help='Build/start this installation without recreating running services')
    start.add_argument('--build', action='store_true', help='Build images before starting stopped services')
    status = sub.add_parser('status', help='Show container state (this is not proof of broker execution)')
    status.add_argument('--json', action='store_true')
    for name in ('stop', 'restart'):
        cmd = sub.add_parser(name, help=f'{name.capitalize()} this installation; asks before pausing position management')
        cmd.add_argument('--yes', action='store_true', help='Acknowledge the management pause')
    logs = sub.add_parser('logs', help='Read logs with known credentials redacted')
    logs.add_argument('--follow', '-f', action='store_true')
    logs.add_argument('--tail', type=int, default=100)
    logs.add_argument('--service', choices=['app', 'mt5-sidecar'])
    logs.add_argument('--since', default='1h')
    logs.add_argument('--level', choices=list(diagnostics.LEVELS), help='Minimum severity, including traceback context')
    logs.add_argument('--contains', help='Case-insensitive literal text filter')
    opened = sub.add_parser('open', help='Open the local panel, or hold a private SSH tunnel to a remote panel')
    opened.add_argument('--settings', action='store_true')
    opened.add_argument('--no-browser', action='store_true', help='Print the address without opening a browser')
    pw = sub.add_parser('password', aliases=['passwd'], help='Reset the panel password live; invalidate old sessions without restarting trading')
    pw.add_argument('--username', help='Also change the panel username')
    pw.add_argument('--stdin', action='store_true', help='Read the new password from stdin instead of a hidden prompt')
    sub.add_parser('config', help='Show paths and redacted configuration; never prints passwords or keys')
    return p


def _setup(args) -> None:
    root = config.home()
    if (root / 'installation.json').exists() or config.env_path().exists():
        raise Error('An installation already exists. Use config/password, or --home for a new isolated installation.')
    if args.remote and args.context:
        raise Error('Choose either --remote or --context.')
    if not args.remote and not args.context and platform.machine().lower() in ('arm64', 'aarch64'):
        raise Error('Local Apple Silicon/ARM MT5 is not automated by this CLI yet. Use your own existing Linux x64 computer/VPS with init --remote user@HOST, or an existing remote context with --context. See docs/CLI.md for the separate Linux ARM/FEX path.')
    source = config.source_root(args.source)
    remote = config.ssh_target(args.remote) if args.remote else None
    if args.from_stdin:
        raw = sys.stdin.read(65_537)
        if len(raw) > 65_536:
            raise Error('Setup input is too large.')
        try:
            values = json.loads(raw)
            if not isinstance(values, dict):
                raise ValueError()
        except ValueError as e:
            raise Error('Expected a JSON object on stdin; no configuration was written.') from e
    else:
        banner()
        say('  1 / 3  Local dashboard', '1;34')
        user = ask('Admin username', 'operator')
        admin_password = password()
        say('\n  2 / 3  MetaTrader account', '1;34')
        say('  Start with a demo account. Use the exact broker server name.')
        values = {'admin_user': user, 'admin_password': admin_password,
                  'mt5_server': ask('Broker server'), 'mt5_login': ask('MT5 account number'),
                  'mt5_password': ask('MT5 password', secret=True)}
        say('\n  3 / 3  Telegram application (optional)', '1;34')
        if ask('Enter Telegram API credentials now? [y/N]').lower() in ('y', 'yes'):
            values['telegram_api_id'] = ask('Telegram API ID')
            values['telegram_api_hash'] = ask('Telegram API hash', secret=True)
    expected = ('admin_user', 'admin_password', 'mt5_server', 'mt5_login', 'mt5_password')
    if any(not isinstance(values.get(key), (str, int)) or isinstance(values.get(key), bool) or not str(values[key]) for key in expected):
        raise Error('Supply admin_user, admin_password, mt5_server, mt5_login and mt5_password.')
    values = {k: str(v) for k, v in values.items()}
    validate_password(values['admin_password'])
    user = values['admin_user']
    if ':' in user or len(user) > 128 or any(ord(c) < 32 for c in user):
        raise Error('Use an admin username without colons or control characters.')
    if not values['mt5_login'].isascii() or not values['mt5_login'].isdigit() or int(values['mt5_login']) <= 0:
        raise Error('MT5 account number must be a positive integer.')
    api_id, api_hash = values.get('telegram_api_id', ''), values.get('telegram_api_hash', '')
    if bool(api_id) != bool(api_hash) or (api_id and (not api_id.isascii() or not api_id.isdigit() or int(api_id) <= 0)):
        raise Error('Telegram API ID and hash must be supplied together; ID is a positive integer.')
    ident = secrets.token_hex(6)
    if remote:
        user_name, host, port = remote
        remote_host = f'[{host}]' if ':' in host else host
        endpoint = f'ssh://{user_name}@{remote_host}:{port}'
        context = 'lvct-' + ident
    else:
        endpoint = None
        context = config.context_name(args.context) if args.context else (
            runtime.run(['docker', 'context', 'show']).stdout.strip() if shutil.which('docker') else 'default')
        config.context_name(context)
    probe_endpoint = endpoint or runtime.ensure_endpoint({'context':context})
    selected_port = ports.select(args.port, probe_endpoint)
    state = {'version': 1, 'id': ident, 'project': 'lvct-' + ident, 'source': str(source),
             'context': context, 'port': selected_port}
    bootstrap_path=root/'bootstrap.json'
    if bootstrap_path.is_file():
        installed=config.read_json(bootstrap_path)
        if installed.get('source')==str(source):state['revision']=installed.get('revision','local')
    project = state['project']
    environment = {
        'SECRET_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        'SIDECAR_TOKEN': secrets.token_urlsafe(48), 'SP_AUTH_USER': user,
        'SP_AUTH_PASSWORD': values['admin_password'], 'SP_ALLOW_UNAUTHENTICATED': 'false',
        'SP_ALLOWED_HOSTS': 'localhost,127.0.0.1', 'MT5_SERVER': values['mt5_server'],
        'MT5_LOGIN': values['mt5_login'], 'MT5_PASSWORD': values['mt5_password'],
        'SIDECAR_URL': 'http://mt5-sidecar:18812', 'DATA_DIR': '/data', 'FRONTEND_DIST': '/app/static',
        'LVCT_ENV_FILE': str(config.env_path()), 'LVCT_PORT': str(selected_port),
        'LVCT_APP_IMAGE': project + '-app:local', 'LVCT_SIDECAR_IMAGE': project + '-mt5:local',
        'LVCT_DATA_VOLUME': project + '_data', 'LVCT_MT5_VOLUME': project + '_mt5', 'LVCT_X11_VOLUME': project + '_x11',
    }
    if api_id:
        environment.update(TELEGRAM_API_ID=api_id, TELEGRAM_API_HASH=api_hash)
    # Validate all values before creating any context or writing a partial secret file.
    if any(any(ord(c) < 32 for c in v) for v in environment.values()):
        raise Error('Setup fields cannot contain control characters.')
    if endpoint:
        runtime.run(['docker', 'context', 'create', context, '--docker', 'host=' + endpoint])
    config.write_env(environment)
    config.secure_write(root / 'installation.json', json.dumps(state, indent=2) + '\n')
    say('  Configuration saved privately. Nothing was started.', '32')
    say(f'  Panel port: {selected_port} (loopback only).')
    say('  Next: lvct doctor → lvct start → lvct open')
    say('  In Settings connect Telegram, choose a channel and AI provider; review demo account and limits before arming.')


def _doctor(args) -> int:
    checks = [{'name':'python','state':'ok','message':sys.version.split()[0]}]
    state = None
    try:
        path = config.home() / 'installation.json'
        if path.exists():
            state = config.load()
            runtime.checked_env(state)
            checks.append({'name':'configuration','state':'ok'})
            if (config.home()/'maintenance.json').exists():
                checks.append({'name':'update_recovery','state':'error','message':'Run lvct update --rollback.'})
        else:
            checks.append({'name':'configuration','state':'warning','message':'Run lvct init after checking dependencies.'})
        target=state or {'context':config.context_name(args.context or runtime.run(['docker','context','show']).stdout.strip())}
        if args.context:target={**target,'context':config.context_name(args.context)}
        data=runtime.check_engine(target,display=False)
        checks.append({'name':'docker_compose','state':'ok','message':str(data.get('ServerVersion',''))})
        if data.get('MemTotal',0)<4*1024**3:
            checks.append({'name':'memory','state':'warning','message':'Allocate at least 4 GiB to Docker for Wine builds.'})
        if state and not args.context:
            rows=runtime.services(state)
            if any(x.get('State')=='running' for x in rows):
                checks.extend(diagnostics.health(state)['checks'])
            else:
                endpoint=runtime.ensure_endpoint(state)
                ports.select(state['port'],endpoint)
                checks.append({'name':'panel_port','state':'ok','message':str(state['port'])})
                checks.append({'name':'runtime','state':'warning','message':'Stopped; doctor does not start services.'})
    except (Error,OSError,ValueError) as e:
        checks.append({'name':'diagnostic','state':'error','message':str(e) if isinstance(e,Error) else 'Configuration or probe failed.'})
    value={'schema':1,'status':'failed' if any(x['state']=='error' for x in checks) else 'passed','checks':checks}
    if args.json:print(json.dumps(value))
    else:
        for item in checks:say(f"  {item['name']:<18} {item['state']:<8} {item.get('message','')}")
    return 1 if value['status']=='failed' else 0


def _deps(args) -> None:
    banner()
    plan, detail = runtime.dependency_plan()
    say(f"  Python {sys.version.split()[0]}: OK")
    say('  Docker CLI: ' + ('found' if shutil.which('docker') else 'missing'))
    say('  ' + detail)
    if not args.install:
        say('  To install missing Docker: lvct deps --install')
        return
    if shutil.which('docker'):
        raise Error('Docker is already installed; no package replacement was attempted. Use lvct doctor to diagnose it.')
    if sys.platform.startswith('linux'):
        say('  Plan: download https://get.docker.com and run the official Docker Engine installer.')
        say('  This installs system packages and may require sudo; no user is added to the Docker group.')
        confirm('Install Docker Engine and Compose?', args.yes)
        with tempfile.TemporaryDirectory(prefix='lvct-docker-') as temporary:
            path = Path(temporary) / 'get-docker.sh'
            with urlopen('https://get.docker.com', timeout=30) as response:
                data = response.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise Error('Docker installer is unexpectedly large.')
                path.write_bytes(data)
            command = ['sh', str(path)]
            if hasattr(os, 'geteuid') and os.geteuid() != 0:
                command = ['sudo', *command]
            code = subprocess.call(command, env=runtime.child_env())
    else:
        if not plan or not shutil.which(plan[0]):
            raise Error('Install Docker Desktop from the official page above, then run lvct doctor.')
        say('  Plan: ' + ' '.join(plan))
        confirm('Install Docker Desktop?', args.yes)
        code = subprocess.call(plan, env=runtime.child_env())
    if code:
        raise Error('Dependency installation did not finish; inspect the installer output.')
    say('  Installer completed. Start Docker, complete any OS/WSL setup, then run lvct doctor.', '32')


def _open(state: dict, args) -> int:
    url = f"http://127.0.0.1:{state['port']}" + ('/settings' if args.settings else '/')
    endpoint = runtime.context_host(state)
    if endpoint.startswith('ssh://'):
        user, host, port = config.ssh_target(endpoint)
        forward = f"127.0.0.1:{state['port']}:127.0.0.1:{state['port']}"
        target = f'{user}@{host}'
        say('  Private SSH tunnel: ' + url + ' (keep this command open; Ctrl+C closes only the tunnel).')
        if not args.no_browser:
            # Opening the URL before the SSH handshake completes is deliberately avoided.
            say('  Open this address once SSH reports the connection is ready.')
        return subprocess.call(['ssh', '-N', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30',
                                '-L', forward, '-p', str(port), '--', target], env=runtime.child_env())
    if not endpoint.startswith(('unix://', 'npipe://')):
        raise Error('Panel opening supports local Docker and SSH contexts; unauthenticated TCP is not used.')
    say('  ' + url)
    if not args.no_browser:
        webbrowser.open(url)
    return 0


def dispatch(args) -> int:
    from .operations import lock
    with lock() if args.command in ('init','setup','start','stop','restart','password','passwd','update') else nullcontext():
        return _dispatch(args)


def _dispatch(args) -> int:
    if args.command in ('init', 'setup'):
        _setup(args)
        return 0
    if args.command == 'deps':
        _deps(args)
        return 0
    if args.command == 'doctor':
        return _doctor(args)
    if args.command == 'report':
        from . import reports
        try:state=config.load()
        except (OSError,Error,ValueError):state=None
        reports.command(state,args)
        return 0
    state = config.load()
    if args.command == 'health':
        if not 5<=args.interval<=300:raise Error('Use --interval 5..300 seconds.')
        while True:
            value=diagnostics.health(state)
            if args.json:print(json.dumps(value),flush=True)
            else:diagnostics.print_health(value)
            if not args.watch:return 0 if value['status']=='healthy' else 1
            time.sleep(args.interval)
    elif args.command == 'errors':
        try:
            value=diagnostics.errors(state,tail=args.tail,since=args.since,service=args.service)
        except Error:
            value={'schema':1,'error_count':0,'groups':[],'runtime_logs_available':False}
        value['cli_errors']=events.recent()
        if args.json:print(json.dumps(value))
        else:
            say(f"  Runtime errors: {value['error_count']} · recent CLI failures: {len(value['cli_errors'])}")
            if value.get('runtime_logs_available') is False:say('  Runtime logs unavailable; check lvct doctor. Local CLI events are shown below.','33')
            for group in value['groups']:
                say(f"\n  {group['code']} ×{group['count']}",'33')
                say('  '+group['action'])
                say(group['sample'])
            for event in value['cli_errors'][-5:]:say(f"  {event['time']} {event['level']} {event['command']}: {event['message']}")
        return 1 if value.get('runtime_logs_available') is False else 0
    elif args.command == 'update':
        if args.source and not args.apply:raise Error('--source requires --apply.')
        if args.rollback:updates.rollback(state)
        elif args.apply:updates.apply(state,ref=args.ref,source=args.source)
        else:
            value=updates.metadata(state,args.ref)
            if args.json:print(json.dumps(value))
            else:
                say('  Public revision: '+value['available_revision'])
                if not value['compatible']:say('  The public revision does not include this CLI yet. Publish the new source before using remote updates.','33')
                else:say('  Update available. Use update --apply during a planned stop.' if value['update_available'] else '  Already current.')
    elif args.command == 'config':
        values = config.read_env()
        say(json.dumps(state, indent=2))
        say(config.redact(json.dumps(values, indent=2, ensure_ascii=False), values))
    elif args.command == 'status':
        rows = runtime.services(state)
        safe = [{k: row.get(k, '') for k in ('Service', 'State', 'Health')} for row in rows]
        if args.json:
            print(json.dumps(safe))
        elif not safe:
            say('  Stopped / not started. Configuration and volumes are retained.')
        else:
            banner()
            for row in safe:
                say(f"  {row['Service']:<16} {row['State']:<12} {row['Health']}", '32' if row['State'] == 'running' else '33')
            say('  Container health does not confirm broker identity or order execution.')
    elif args.command == 'start':
        updates.guard_start()
        runtime.check_engine(state)
        running = {x.get('Service') for x in runtime.services(state) if x.get('State') == 'running'}
        if {'app', 'mt5-sidecar'} <= running:
            say('  Already running. No container was replaced.', '32')
            return 0
        runtime.prepare_volumes(state)
        command = ['up', '-d', '--no-recreate']
        if args.build:
            command.append('--build')
        code = runtime.stream(state, runtime.compose(state, *command))
        if code:
            return code
        say('  Services started; Wine/MT5 can take several minutes. Use lvct status and lvct logs.')
        say('  Open Settings to finish Telegram and AI setup; trading is armed only in the dashboard.')
    elif args.command in ('stop', 'restart'):
        confirm(f"{args.command.capitalize()} this installation? Position management pauses; broker positions remain", args.yes)
        result = runtime.execute(state, args.command, timeout=None)
        say(config.redact(result.stdout, config.read_env()))
    elif args.command == 'logs':
        if not args.follow:
            for line in diagnostics.filtered(diagnostics.read_logs(state,tail=args.tail,since=args.since,service=args.service),args.level,args.contains):say(line)
        else:
            if not 1<=args.tail<=2000:raise Error('Use --tail 1..2000.')
            diagnostics.validate_since(args.since)
            command=['logs','--no-color','--timestamps','--tail',str(args.tail),'--since',args.since,'--follow']
            if args.service:command.append(args.service)
            return runtime.stream(state,runtime.compose(state,*command),diagnostics.stream_filter(args.level,args.contains))
    elif args.command in ('password', 'passwd'):
        new = sys.stdin.read(1026).removesuffix('\n') if args.stdin else password()
        validate_password(new)
        payload = {'password': new}
        if args.username is not None:
            if not args.username or ':' in args.username or len(args.username) > 128 or any(ord(c) < 32 for c in args.username):
                raise Error('Invalid admin username.')
            payload['username'] = args.username
        if not any(x.get('Service') == 'app' and x.get('State') == 'running' for x in runtime.services(state)):
            raise Error('The app must be running to rotate its password. Before the first start, use the credentials from init.')
        result = runtime.execute(state, 'exec', '-T', 'app', 'python', '-m', 'app.cli.admin', 'password',
                                 data=json.dumps(payload), timeout=30)
        say(result.stdout.strip(), '32')
    elif args.command == 'open':
        return _open(state, args)
    return 0


def menu() -> int:
    actions = {'1': ['init'], '2': ['doctor'], '3': ['start'], '4': ['status'], '5': ['logs'],
               '6': ['open'], '7': ['password'], '8': ['config'], '9': ['deps'], '10':['health'], '11':['errors'], '12':['update'], '13':['report']}
    while True:
        banner()
        say('  1 Setup     2 Doctor    3 Start\n  4 Status    5 Logs      6 Open panel\n  7 Password  8 Config    9 Dependencies\n  10 Health   11 Errors   12 Updates   13 Support report\n  0 Exit')
        choice = ask('Choose', '0')
        if choice == '0':
            return 0
        if choice not in actions:
            say('  Choose a listed action.', '33')
            continue
        try:
            dispatch(parser().parse_args(actions[choice]))
        except Error as e:
            say('  ' + str(e), '31')
        ask('Press Enter to return', '')


def main(argv: list[str] | None = None) -> int:
    for output in (sys.stdout, sys.stderr):
        if not output.isatty() and hasattr(output, 'reconfigure'):
            output.reconfigure(encoding='utf-8', errors='replace')
    p = parser()
    args = p.parse_args(argv)
    if args.home:
        os.environ['LVCT_HOME'] = str(args.home.expanduser().absolute())
    if args.no_color:
        os.environ['NO_COLOR'] = '1'
    try:
        if not args.command:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                p.print_help()
                return 0
            return menu()
        return dispatch(args)
    except KeyboardInterrupt:
        say('\n  Cancelled.')
        return 130
    except (Error, OSError, ValueError) as e:
        # Do not echo arbitrary parser/JSON/environment values that might be credentials.
        message = str(e) if isinstance(e, Error) else 'Operation failed; check paths, input and permissions.'
        events.record(args.command or 'menu',message)
        say('  ' + message, '31')
        return 1
    except Exception as e:
        events.record(args.command or 'menu','Unexpected '+type(e).__name__,critical=True)
        say('  Unexpected failure. See lvct errors and lvct report; no automatic retry was sent.','31')
        return 1
