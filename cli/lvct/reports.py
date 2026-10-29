"""Voluntary minimised diagnostics: save, review, then explicitly upload over HTTPS."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import platform
import re
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
import uuid

from . import __version__, config, diagnostics, events
from .console import Error, confirm, say
from .privacy import scrub

ENDPOINT='https://lvenc.site/api/support'


def validate(value):
    """Shared strict allowlist; both sender and receiver validate before storing/sending."""
    keys={'schema','id','created_at','client','health','issues','logs','cli_errors','note','contact'}
    if not isinstance(value,dict) or set(value)!=keys or value['schema']!=1:raise ValueError('Invalid report schema')
    if str(uuid.UUID(value['id']))!=value['id']:raise ValueError('Invalid report ID')
    if not isinstance(value['created_at'],str) or len(value['created_at'].replace('Z','+00:00'))>40:raise ValueError('Invalid time')
    datetime.fromisoformat(value['created_at'].replace('Z','+00:00'))
    if not isinstance(value['client'],dict) or set(value['client'])!={'version','python','os','arch'}:raise ValueError('Invalid client')
    for item in value['client'].values():
        if not isinstance(item,str) or not re.fullmatch(r'[A-Za-z0-9_.+ -]{1,64}',item):raise ValueError('Invalid client value')
    health=value['health']
    if not isinstance(health,dict) or set(health)!={'status','checks'} or health['status'] not in ('healthy','degraded','unhealthy','unknown'):raise ValueError('Invalid health')
    if not isinstance(health['checks'],list) or len(health['checks'])>30:raise ValueError('Invalid checks')
    for item in health['checks']:
        if not isinstance(item,dict) or set(item)!={'name','state'} or not re.fullmatch(r'[a-z0-9_-]{1,40}',item['name']) or item['state'] not in ('ok','warning','error','starting'):raise ValueError('Invalid check')
    if not isinstance(value['issues'],list) or len(value['issues'])>15:raise ValueError('Invalid issues')
    for item in value['issues']:
        if not isinstance(item,dict) or set(item)!={'code','count'} or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}',item['code']) or type(item['count']) is not int or not 0<=item['count']<=2000:raise ValueError('Invalid issue')
    for key,count,size in [('logs',50,1000),('cli_errors',10,1000)]:
        if not isinstance(value[key],list) or len(value[key])>count or any(not isinstance(x,str) or len(x)>size for x in value[key]):raise ValueError('Invalid logs')
    if not isinstance(value['note'],str) or len(value['note'])>1000:raise ValueError('Invalid note')
    if value['contact'] is not None and (not isinstance(value['contact'],str) or len(value['contact'])>254 or not re.fullmatch(r'[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+',value['contact'])):raise ValueError('Invalid contact email')
    if len(json.dumps(value,ensure_ascii=False).encode())>60_000:raise ValueError('Report is too large')
    return value


def collect(state,note='',contact=None):
    try:values=config.read_env()
    except (OSError,Error,ValueError):values={}
    health=diagnostics.health(state) if state else {'status':'unknown','checks':[]}
    try:
        if not state:raise Error('Not configured')
        lines=diagnostics.read_logs(state,tail=300,since='1h')
        records=diagnostics.filtered(lines,'error')[-50:]
        issues=diagnostics.errors_from_lines(lines)
    except (Error,OSError,ValueError):
        records=['Runtime logs unavailable; inspect Docker/SSH access.']
        issues={'groups':[]}
    result={'schema':1,'id':str(uuid.uuid4()),'created_at':datetime.now(timezone.utc).isoformat(),
            'client':{'version':__version__,'python':platform.python_version(),'os':platform.system(),'arch':platform.machine()},
            'health':{'status':health['status'],'checks':[{'name':x['name'],'state':x['state']} for x in health['checks']]},
            'issues':[{'code':g['code'],'count':g['count']} for g in issues['groups']],
            'logs':[scrub(line,values,limit=1000) for line in records],
            'cli_errors':[scrub(json.dumps(x),values,limit=1000) for x in events.recent()[-10:]],
            'note':scrub(note,values,limit=1000),'contact':contact}
    while len(json.dumps(result,ensure_ascii=False).encode())>55_000:
        if result['logs']:result['logs'].pop(0)
        elif result['cli_errors']:result['cli_errors'].pop(0)
        else:break
    return validate(result)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise Error('Support redirects are refused.')


def request(path,payload=None):
    body=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode() if payload is not None else None
    try:
        req=Request(ENDPOINT+path,data=body,headers={'User-Agent':'lvct/'+__version__,'Content-Type':'application/json'})
        with build_opener(NoRedirect).open(req,timeout=20) as response:
            # Never follow reports to a different origin through a redirect.
            if response.geturl()!=ENDPOINT+path:raise Error('Support redirects are refused.')
            return json.loads(response.read(4097))
    except HTTPError as exc:
        raise Error(f'Support returned HTTP {exc.code}. The local report is retained; do not retry in a tight loop.') from exc
    except (URLError,ValueError) as exc:
        raise Error('Support is unavailable. Keep the saved report and retry later.') from exc


def send(value):
    validate(value)
    challenge=request('/challenge')
    token,bits=challenge['token'],challenge['bits']
    if not isinstance(token,str) or len(token)>1024 or type(bits) is not int or not 12<=bits<=20:raise Error('Invalid support challenge.')
    deadline=time.monotonic()+15
    nonce=0
    target=1<<(256-bits)
    while int.from_bytes(hashlib.sha256((token+':'+str(nonce)).encode()).digest(),'big')>=target:
        nonce+=1
        if nonce%4096==0 and time.monotonic()>deadline:raise Error('Support challenge timed out; saved report retained.')
    result=request('/reports',{'challenge':token,'nonce':nonce,'report':value})
    if result.get('id')!=value['id'] or result.get('accepted') is not True:raise Error('Support did not acknowledge this report; keep the file.')
    say('  Support accepted report '+value['id']+'. Notification delivery is queued.', '32')


def command(state,args):
    if args.file:
        if not args.send:raise Error('--file is used with --send after reviewing a saved report.')
        value=validate(config.read_json(args.file.expanduser()))
    else:
        value=collect(state,args.note,args.contact)
        path=config.home()/'reports'/(value['id']+'.json')
        config.secure_write(path,json.dumps(value,indent=2,ensure_ascii=False)+'\n')
        say('  Report saved privately: '+str(path))
        say('  Review the JSON before sending. It contains recent error text; automatic redaction is best effort.')
        say('  Send: lvct report --send --file '+str(path))
    if args.send:
        confirm('Send this report to LV support? Logs are retained up to 14 days; optional contact email is included',args.yes)
        send(value)
