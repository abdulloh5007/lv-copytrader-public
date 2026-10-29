"""Read-only health and actionable error groups; no automatic trading repair."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import re

from . import __version__, config, runtime
from .console import Error, say

LEVELS = {'debug': 10, 'info': 20, 'warning': 30, 'error': 40, 'critical': 50}
SEVERITY = re.compile(r'\b(CRITICAL|FATAL|ERROR|WARNING|WARN|INFO|DEBUG)\b', re.I)
RULES = [
    ('configuration', r'SECRET_KEY|Set a strong SIDECAR_TOKEN|validation error|missing.*credential', 'Check lvct doctor and the named setting. Do not replace an existing encryption key.'),
    ('authentication', r'auth_login_failed|invalid_credentials|unauthorized|too_many_attempts', 'Check the username or use lvct password. A 429 requires waiting; do not retry in a tight loop.'),
    ('mt5_ipc', r'IPC (?:send|recv|timeout)|-1000[125]|terminal.*not.*found', 'Read the MT5 sidecar logs and the runbook. Inspect terminal/server selection before any restart.'),
    ('algo_disabled', r'10027|10026|algo.*disabled|trading.*disabled', 'Verify terminal Algo Trading and broker permissions; do not send an order as a diagnostic.'),
    ('broker_connection', r'10031|sidecar unreachable|rpc.*failed|connection.*refused|timed out|timeout', 'Check lvct health and network/server settings. Inspect order history before retrying any ambiguous trade.'),
    ('provider', r'provider.*error|rate_limit|insufficient_quota|api.*(?:400|401|403|429)|anthropic.*error', 'Check provider settings, credit and the model capability probe in the panel.'),
    ('telegram', r'Telegram.*(?:error|disconnect)|FloodWait|AuthKey|SESSION_REVOKED', 'Check Telegram connection/session in Settings. Observe FloodWait delays.'),
    ('storage', r'database.*locked|disk.*full|No space left|Permission denied|decrypt|InvalidToken', 'Check disk/volume access and the original encryption key. Do not delete the database or regenerate SECRET_KEY.'),
]

# Executed with Docker-owner access. Only the allowlisted booleans leave this process.
HEALTH_PROBE = '''import base64,json,pathlib,time,urllib.request
from app.config import settings
from app.util.auth import _sign
from app.util.auth_state import SessionGuard
try:
 from app.util.admin_credentials import AdminCredentials
 credentials=AdminCredentials(pathlib.Path(settings.DATA_DIR)/'admin-credentials.json',lambda:(settings.SP_AUTH_USER,settings.SP_AUTH_PASSWORD)).current
except ImportError:
 credentials=lambda:(settings.SP_AUTH_USER,settings.SP_AUTH_PASSWORD)
session_file=pathlib.Path(settings.DATA_DIR)/'auth-sessions.json'
if not session_file.is_file():raise RuntimeError('Session state unavailable for authenticated health probe')
guard=SessionGuard(session_file,lambda:settings.SECRET_KEY,credentials)
r=urllib.request.Request('http://127.0.0.1:8000/api/health',headers={'Cookie':'sp_auth='+_sign(guard.secret,int(time.time()))})
with urllib.request.urlopen(r,timeout=5) as response: data=json.load(response)
allowed={'app':data.get('ok') is True,'armed':data.get('armed') is True}
for key in ['mt5','telegram','provider']:
 value=(data.get('status') or {}).get(key,{})
 allowed[key]={'ok':value.get('ok') is True}
print(json.dumps(allowed))
'''


def log_records(lines: list[str]) -> list[dict]:
    records, current = [], None
    for line in lines:
        match = SEVERITY.search(line)
        context=re.sub(r'^.*?\|\s*','',line)
        context=re.sub(r'^\d{4}-\d\d-\d\dT\S+\s+','',context)
        if match:
            level = {'WARN':'WARNING','FATAL':'CRITICAL'}.get(match.group(1).upper(), match.group(1).upper())
            current = {'level':level, 'text':line[:4000]}
            records.append(current)
        elif current is not None and (context.lstrip().startswith(('Traceback','File ','raise ','^')) or re.search(r'\b\w+(?:Error|Exception):',line)):
            current['text'] = (current['text'] + '\n' + line)[:8000]
        else:
            current = None
    return records


def filtered(lines: list[str], level: str | None = None, contains: str | None = None) -> list[str]:
    if not level and not contains:
        return lines
    if level:
        records = log_records(lines)
        lines = [r['text'] for r in records if LEVELS[r['level'].lower()] >= LEVELS[level]]
    if contains:
        lines = [line for line in lines if contains.casefold() in line.casefold()]
    return lines


def read_logs(state: dict, *, tail: int = 300, since: str = '1h', service: str | None = None) -> list[str]:
    if not 1 <= tail <= 2000 or not re.fullmatch(r'[1-9]\d{0,3}[smhd]|\d{4}-\d\d-\d\dT[0-9:.+Z-]{5,25}', since):
        raise Error('Use --tail 1..2000 and --since such as 30m, 1h, 2d or an ISO timestamp.')
    args = ['logs','--no-color','--timestamps','--tail',str(tail),'--since',since]
    if service:
        args.append(service)
    result = runtime.execute(state,*args,timeout=20)
    values = config.read_env()
    return [config.redact(line,values) for line in result.stdout.splitlines()]


def errors(state: dict, *, tail: int = 300, since: str = '1h', service: str | None = None) -> dict:
    return errors_from_lines(read_logs(state,tail=tail,since=since,service=service))


def errors_from_lines(lines):
    entries = [r for r in log_records(lines) if LEVELS[r['level'].lower()] >= 40]
    groups = {}
    for entry in entries:
        code, instruction = 'unclassified', 'Read the surrounding log context; collect a support report if the cause remains unclear.'
        for name, pattern, help_text in RULES:
            if re.search(pattern,entry['text'],re.I):
                code,instruction=name,help_text
                break
        group = groups.setdefault(code,{'code':code,'count':0,'action':instruction,'sample':entry['text'][:2000]})
        group['count'] += 1
        group['sample'] = entry['text'][:2000]
    return {'schema':1,'error_count':len(entries),'groups':list(groups.values())}


def health(state: dict) -> dict:
    checks = []
    try:
        runtime.checked_env(state)
        checks.append({'name':'configuration','state':'ok'})
        rows = runtime.services(state)
        by_name = {r.get('Service'):r for r in rows}
        for name in ('app','mt5-sidecar'):
            row = by_name.get(name,{})
            running = row.get('State') == 'running'
            good = running and row.get('Health') in ('healthy','')
            checks.append({'name':name,'state':'ok' if good else ('starting' if running and row.get('Health')=='starting' else 'error')})
        if by_name.get('app',{}).get('State') == 'running':
            result=runtime.execute(state,'exec','-T','app','python','-c',HEALTH_PROBE,timeout=12)
            value=json.loads(result.stdout)
            if not isinstance(value,dict):raise ValueError()
            checks.append({'name':'panel_api','state':'ok'})
            if value.get('app') is not True:
                checks.append({'name':'application_runtime','state':'error'})
            for name in ('mt5','telegram','provider','notifier','database'):
                item=value.get(name)
                if isinstance(item,dict):good=any(item.get(k) is True for k in ('ok','connected','ready'))
                else:good=item in (True,'ok')
                if item is not None:checks.append({'name':name,'state':'ok' if good else 'warning'})
    except (Error,ValueError,OSError):
        checks.append({'name':'diagnostic_probe','state':'error','message':'Probe failed; run lvct doctor and lvct errors for details.'})
    status='unhealthy' if any(x['state']=='error' for x in checks) else ('degraded' if any(x['state']!='ok' for x in checks) else 'healthy')
    return {'schema':1,'status':status,'cli_version':__version__,'checked_at':datetime.now(timezone.utc).isoformat(),'checks':checks}


def print_health(report: dict) -> None:
    say('  Health: '+report['status'], '32' if report['status']=='healthy' else '33')
    for check in report['checks']:
        say(f"  {check['name']:<18} {check['state']}")


def validate_since(since: str):
    if not re.fullmatch(r'[1-9]\d{0,3}[smhd]|\d{4}-\d\d-\d\dT[0-9:.+Z-]{5,25}',since):
        raise Error('Use --since such as 30m, 1h, 2d or an ISO timestamp.')


def stream_filter(level=None, contains=None):
    # Retain severity for traceback lines while following, without buffering forever.
    active=False
    def transform(line):
        nonlocal active
        match=SEVERITY.search(line)
        context=re.sub(r'^.*?\|\s*','',line)
        context=re.sub(r'^\d{4}-\d\d-\d\dT\S+\s+','',context)
        if match:
            name={'WARN':'WARNING','FATAL':'CRITICAL'}.get(match.group(1).upper(),match.group(1).upper())
            active=not level or LEVELS[name.lower()]>=LEVELS[level]
        elif not (context.lstrip().startswith(('Traceback','File ','raise ','^')) or re.search(r'\b\w+(?:Error|Exception):',line)):
            active=not level
        return line if active and (not contains or contains.casefold() in line.casefold()) else None
    return transform
