"""Bounded encrypted support inbox and durable, independently retried owner alerts."""
from __future__ import annotations
import asyncio
import base64
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import smtplib
import sqlite3
import ssl
import time
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request as WebRequest
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from lvct.privacy import scrub
from lvct.reports import validate

RETENTION=14*86400
BODY_LIMIT=65536
log=logging.getLogger("lvct.support")


class Inbox:
    def __init__(self,path,key,*,bits=18,daily_limit=20):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.is_symlink():raise RuntimeError('Symlinked support database')
        self.cipher=Fernet(key)
        self.secret=hashlib.sha256(key.encode() if isinstance(key,str) else key).digest()
        self.bits=bits
        self.daily_limit=daily_limit
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY,created REAL,ip TEXT,payload BLOB,tg INTEGER DEFAULT 0,email INTEGER DEFAULT 0,tg_attempts INTEGER DEFAULT 0,email_attempts INTEGER DEFAULT 0,tg_next REAL DEFAULT 0,email_next REAL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS used(nonce TEXT PRIMARY KEY,expires REAL);
              CREATE TABLE IF NOT EXISTS limits(bucket TEXT PRIMARY KEY,count INTEGER,expires REAL);
            ''')
            columns={row[1] for row in db.execute('PRAGMA table_info(reports)')}
            for name in ('tg_error','email_error'):
                if name not in columns:db.execute('ALTER TABLE reports ADD COLUMN '+name+" TEXT DEFAULT ''")
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=5)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:yield db
        finally:db.close()

    def ip_key(self,ip):
        return hmac.new(self.secret,ip.encode(),hashlib.sha256).hexdigest()

    def budget(self,db,bucket,limit,window,now):
        name=bucket+':'+str(int(now//window))
        db.execute('INSERT INTO limits VALUES(?,0,?) ON CONFLICT DO NOTHING',(name,now+window))
        if db.execute('SELECT count FROM limits WHERE bucket=?',(name,)).fetchone()[0]>=limit:
            raise HTTPException(429,'Support request limit reached; retry later.')
        db.execute('UPDATE limits SET count=count+1 WHERE bucket=?',(name,))

    def challenge(self,ip):
        now=time.time()
        ip=self.ip_key(ip)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM limits WHERE expires<?',(now,))
            self.budget(db,'challenge:'+ip,10,600,now)
            self.budget(db,'challenge:global',1000,3600,now)
        raw=json.dumps({'ip':ip,'exp':int(now)+300,'nonce':secrets.token_hex(16)},separators=(',',':')).encode()
        body=base64.urlsafe_b64encode(raw).decode().rstrip('=')
        signature=hmac.new(self.secret,body.encode(),hashlib.sha256).hexdigest()
        return {'token':body+'.'+signature,'bits':self.bits,'expires_in':300}

    def proof(self,token,nonce,ip):
        if not isinstance(token,str) or len(token)>1024 or type(nonce) is not int or not 0<=nonce<=2**40:raise HTTPException(400,'Invalid challenge.')
        try:
            body,signature=token.split('.')
            expected=hmac.new(self.secret,body.encode(),hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected,signature):raise ValueError()
            value=json.loads(base64.urlsafe_b64decode(body+'='*(-len(body)%4)))
            if value['ip']!=self.ip_key(ip) or not time.time()<value['exp']<=time.time()+301:raise ValueError()
            digest=hashlib.sha256((token+':'+str(nonce)).encode()).digest()
            if int.from_bytes(digest,'big')>=1<<(256-self.bits):raise ValueError()
            return value
        except (ValueError,KeyError,TypeError):raise HTTPException(400,'Invalid or expired challenge.') from None

    def accept(self,ip,payload):
        if not isinstance(payload,dict) or set(payload)!={'challenge','nonce','report'}:raise HTTPException(400,'Invalid report envelope.')
        proof=self.proof(payload['challenge'],payload['nonce'],ip)
        try:value=validate(payload['report'])
        except (ValueError,TypeError,KeyError,AttributeError):raise HTTPException(400,'Invalid report schema.') from None
        # Apply a second independent scrub; contact is an explicit optional reply address.
        for key in ('logs','cli_errors'):value[key]=[scrub(x,limit=1000) for x in value[key]]
        value['note']=scrub(value['note'],limit=1000)
        now=time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM reports WHERE created<?',(now-RETENTION,))
            db.execute('DELETE FROM used WHERE expires<?',(now,))
            db.execute('DELETE FROM limits WHERE expires<?',(now,))
            found=db.execute('SELECT ip FROM reports WHERE id=?',(value['id'],)).fetchone()
            if found:
                if found['ip']!=proof['ip']:raise HTTPException(409,'Report ID conflict.')
                return {'accepted':True,'id':value['id'],'duplicate':True}
            if db.execute('SELECT 1 FROM used WHERE nonce=?',(proof['nonce'],)).fetchone():raise HTTPException(409,'Challenge already used.')
            self.budget(db,'submit:'+proof['ip'],3,3600,now)
            self.budget(db,'submit:global',self.daily_limit,86400,now)
            if db.execute('SELECT count(*) FROM reports').fetchone()[0]>=500:raise HTTPException(503,'Support inbox temporarily full.')
            db.execute('INSERT INTO used VALUES(?,?)',(proof['nonce'],proof['exp']))
            db.execute('INSERT INTO reports(id,created,ip,payload) VALUES(?,?,?,?)',(value['id'],now,proof['ip'],self.cipher.encrypt(json.dumps(value,ensure_ascii=False).encode())))
        return {'accepted':True,'id':value['id']}

    def read(self,ident):
        with self.db() as db:row=db.execute('SELECT payload FROM reports WHERE id=?',(ident,)).fetchone()
        return json.loads(self.cipher.decrypt(row[0])) if row else None

    def notify(self):
        now=time.time()
        # Cleanup also runs without incoming traffic; plaintext reports never touch disk.
        with self.db() as db:
            db.execute('DELETE FROM reports WHERE created<?',(now-RETENTION,))
            rows=db.execute('SELECT * FROM reports ORDER BY created LIMIT 500').fetchall()
        for row in rows:
            report=json.loads(self.cipher.decrypt(row['payload']))
            text=('LVCT support · '+report['id']+'\n'+report['client']['os']+' · CLI '+report['client']['version']+
                  ' · '+report['health']['status']+'\n'+', '.join(x['code']+' ×'+str(x['count']) for x in report['issues'])+
                  '\n'+report['note']+'\n'+('\n'.join(report['logs'][-2:]))[:1800])
            for channel in ('tg','email'):
                if row[channel] or row[channel+'_attempts']>=5 or row[channel+'_next']>now:continue
                try:
                    with self.db() as db:
                        db.execute('BEGIN IMMEDIATE')
                        self.budget(db,'notify:'+channel,20,86400,now)
                except HTTPException:
                    continue
                try:
                    delivered=deliver(channel,text,report['id'])
                    reason='' if delivered else 'transport_rejected_or_not_configured'
                except Exception as exc:
                    reason=type(exc).__name__
                    delivered=False  # Never log SMTP/token error strings or report payloads.
                attempts=row[channel+'_attempts']+1
                if not delivered:
                    log.warning('support_notification_failed channel=%s report=%s attempt=%d reason=%s',channel,report['id'],attempts,reason)
                with self.db() as db:
                    db.execute(f'UPDATE reports SET {channel}=?,{channel}_attempts=?,{channel}_next=?,{channel}_error=? WHERE id=?',
                               (int(delivered),attempts,now+min(3600,60*2**attempts),reason,row['id']))


def deliver(channel,text,ident):
    if channel=='tg':
        token,chat=os.environ.get('SUPPORT_TG_TOKEN'),os.environ.get('SUPPORT_TG_CHAT')
        if not token or not chat:return False
        body=json.dumps({'chat_id':chat,'text':text,'disable_web_page_preview':True}).encode()
        with urlopen(Request('https://api.telegram.org/bot'+token+'/sendMessage',data=body,headers={'Content-Type':'application/json'}),timeout=10) as response:
            return json.loads(response.read(16384)).get('ok') is True
    user,pwd=os.environ.get('SUPPORT_SMTP_USER'),os.environ.get('SUPPORT_SMTP_PASSWORD')
    recipient,sender=os.environ.get('SUPPORT_RECIPIENT'),os.environ.get('SUPPORT_SENDER')
    if not all((user,pwd,recipient,sender)):return False
    message=EmailMessage()
    message['From']=sender
    message['To']=recipient
    message['Subject']='LVCT support report '+ident
    message['Message-ID']='<lvct-support-'+ident+'@'+sender.split('@')[-1]+'>'
    message.set_content(text+'\n\nView full report with the server support admin command. Reports expire after 14 days.')
    with smtplib.SMTP(os.environ['SUPPORT_SMTP_HOST'],int(os.environ.get('SUPPORT_SMTP_PORT','587')),timeout=10) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(user,pwd)
        return not smtp.send_message(message)


def client_ip(request):
    peer=request.client.host if request.client else 'unknown'
    trusted=os.environ.get('SUPPORT_TRUSTED_PROXIES','127.0.0.1').split(',')
    value=request.headers.get('x-lvct-client','') if peer in trusted else peer
    try:return str(ipaddress.ip_address(value))
    except ValueError:raise HTTPException(400,'Invalid client address.') from None


def create_app(inbox=None):
    async def worker(box):
        while True:
            try:await asyncio.to_thread(box.notify)
            except Exception as exc:log.error('support_worker_failed reason=%s',type(exc).__name__)
            await asyncio.sleep(15)
    @asynccontextmanager
    async def lifespan(app):
        box=inbox or Inbox(Path(os.environ.get('SUPPORT_DATA_DIR','/data'))/'support.sqlite3',os.environ['SUPPORT_KEY'])
        app.state.inbox=box
        task=asyncio.create_task(worker(box)) if os.environ.get('SUPPORT_NOTIFICATIONS','1')=='1' else None
        yield
        if task:
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['lvenc.site','127.0.0.1','localhost','testserver'])
    @app.middleware('http')
    async def headers(request,call_next):
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        return response
    @app.get('/api/support/health')
    async def health():return {'ok':True}
    @app.get('/api/support/challenge')
    async def challenge(request:WebRequest):
        return await asyncio.to_thread(request.app.state.inbox.challenge,client_ip(request))
    @app.post('/api/support/reports',status_code=202)
    async def submit(request:WebRequest):
        if request.headers.get('content-type','').split(';')[0]!='application/json':raise HTTPException(415,'JSON required.')
        async def bounded_body():
            body=bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>BODY_LIMIT:raise HTTPException(413,'Report too large.')
            return body
        try:body=await asyncio.wait_for(bounded_body(),timeout=10)
        except asyncio.TimeoutError:raise HTTPException(408,'Report upload timed out.') from None
        try:value=json.loads(body)
        except (ValueError,UnicodeDecodeError,RecursionError):raise HTTPException(400,'Invalid JSON.') from None
        return await asyncio.to_thread(request.app.state.inbox.accept,client_ip(request),value)
    return app

app=create_app()
