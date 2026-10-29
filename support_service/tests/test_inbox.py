import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'cli'))
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from support_service.app import Inbox,create_app


def report():
    return {'schema':1,'id':str(uuid.uuid4()),'created_at':'2026-10-28T00:00:00+00:00',
            'client':{'version':'0.1.0','python':'3.12.1','os':'Linux','arch':'x86_64'},
            'health':{'status':'degraded','checks':[{'name':'mt5','state':'warning'}]},
            'issues':[{'code':'mt5_ipc','count':1}],'logs':['ERROR token=secret-value account 123456789 contact person@example.com'],
            'cli_errors':[],'note':'Synthetic support test','contact':None}


def envelope(box,value=None,ip='192.0.2.1'):
    challenge=box.challenge(ip)
    token=challenge['token']
    nonce=0
    while int.from_bytes(hashlib.sha256((token+':'+str(nonce)).encode()).digest(),'big')>=1<<(256-challenge['bits']):nonce+=1
    return {'challenge':token,'nonce':nonce,'report':value or report()}


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'support.sqlite3'
        self.inbox=Inbox(self.path,Fernet.generate_key(),bits=4,daily_limit=20)

    def test_encrypted_at_rest_redacted_and_idempotent(self):
        payload=envelope(self.inbox)
        result=self.inbox.accept('192.0.2.1',payload)
        self.assertTrue(result['accepted'])
        saved=self.inbox.read(result['id'])
        self.assertNotIn('secret-value',json.dumps(saved))
        self.assertNotIn('123456789',json.dumps(saved))
        with self.inbox.db() as db:
            row=db.execute('SELECT * FROM reports').fetchone()
        self.assertNotIn(b'Synthetic',row['payload'])
        self.assertNotEqual(row['ip'],'192.0.2.1')
        self.assertTrue(self.inbox.accept('192.0.2.1',payload)['duplicate'])
        payload['report']['id']=str(uuid.uuid4())
        with self.assertRaises(HTTPException) as error:self.inbox.accept('192.0.2.1',payload)
        self.assertEqual(error.exception.status_code,409)

    def test_ip_binding_tampering_and_submission_limit(self):
        payload=envelope(self.inbox)
        with self.assertRaises(HTTPException):self.inbox.accept('192.0.2.2',payload)
        token=payload['challenge']
        payload['challenge']=token+'bad'
        with self.assertRaises(HTTPException):self.inbox.accept('192.0.2.1',payload)
        for _ in range(3):self.inbox.accept('192.0.2.1',envelope(self.inbox))
        with self.assertRaises(HTTPException) as error:self.inbox.accept('192.0.2.1',envelope(self.inbox))
        self.assertEqual(error.exception.status_code,429)

    def test_strict_schema_and_body_limit_no_inbox_read_api(self):
        with patch.dict(os.environ,{'SUPPORT_NOTIFICATIONS':'0','SUPPORT_TRUSTED_PROXIES':'testclient'}),TestClient(create_app(self.inbox)) as client:
            headers={'X-LVCT-Client':'192.0.2.1'}
            self.assertEqual(client.get('/api/support/health').status_code,200)
            payload=envelope(self.inbox)
            payload['report']['env']='must not store'
            self.assertEqual(client.post('/api/support/reports',json=payload,headers=headers).status_code,400)
            self.assertEqual(client.post('/api/support/reports',content=b' '*65537,headers={**headers,'Content-Type':'application/json'}).status_code,413)
            self.assertEqual(client.get('/api/support/reports').status_code,405)
            self.assertEqual(client.get('/api/support/reports/private').status_code,404)
            self.assertEqual(client.get('/api/support/health',headers={'Host':'evil.example'}).status_code,400)

    def test_outbox_retries_only_failed_channel_and_retention(self):
        payload=envelope(self.inbox)
        self.inbox.accept('192.0.2.1',payload)
        with patch('support_service.app.deliver',side_effect=lambda channel,*_:channel=='tg') as deliver:
            self.inbox.notify()
            self.inbox.notify()
            self.assertEqual(deliver.call_count,2)
        with self.inbox.db() as db:
            row=db.execute('SELECT * FROM reports').fetchone()
            self.assertEqual(row['tg'],1)
            self.assertEqual(row['email'],0)
            db.execute('UPDATE reports SET email_next=0')
        with patch('support_service.app.deliver',return_value=True) as deliver:
            self.inbox.notify()
            self.assertEqual(deliver.call_args.args[0],'email')
            self.assertEqual(deliver.call_count,1)
        with self.inbox.db() as db:db.execute('UPDATE reports SET created=?',(time.time()-15*86400,))
        with patch('support_service.app.deliver') as deliver:
            self.inbox.notify()
            deliver.assert_not_called()
        self.assertIsNone(self.inbox.read(payload['report']['id']))

    def test_valid_http_report_and_untrusted_forwarded_ip_are_checked(self):
        # Untrusted peers cannot choose the identity bound into a challenge.
        with patch.dict(os.environ,{'SUPPORT_NOTIFICATIONS':'0','SUPPORT_TRUSTED_PROXIES':'127.0.0.1'}),TestClient(create_app(self.inbox),client=('192.0.2.1',30000)) as client:
            payload=envelope(self.inbox)
            response=client.post('/api/support/reports',json=payload,headers={'X-LVCT-Client':'192.0.2.99'})
            self.assertEqual(response.status_code,202,response.text)
            self.assertEqual(response.json()['id'],payload['report']['id'])
            self.assertTrue(response.headers['Cache-Control']=='no-store')

    def test_expired_challenge_and_global_limit(self):
        payload=envelope(self.inbox)
        with patch('support_service.app.time.time',return_value=time.time()+301):
            with self.assertRaises(HTTPException):self.inbox.accept('192.0.2.1',payload)
        self.inbox.daily_limit=1
        self.inbox.accept('192.0.2.1',envelope(self.inbox))
        with self.assertRaises(HTTPException) as error:self.inbox.accept('192.0.2.2',envelope(self.inbox,ip='192.0.2.2'))
        self.assertEqual(error.exception.status_code,429)

    def test_independent_notification_attempt_budget(self):
        self.inbox.accept('192.0.2.1',envelope(self.inbox))
        with self.inbox.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for _ in range(20):self.inbox.budget(db,'notify:email',20,86400,time.time())
        with patch('support_service.app.deliver',return_value=True) as deliver:
            self.inbox.notify()
            self.assertEqual(deliver.call_count,1)
            self.assertEqual(deliver.call_args.args[0],'tg')
