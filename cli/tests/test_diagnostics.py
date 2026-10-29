import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import test_cli
ROOT=test_cli.ROOT
from lvct import cli, config, runtime, diagnostics, ports, privacy, reports, updates
from lvct.console import Error


class DiagnosticsTests(unittest.TestCase):
    setUp=test_cli.CLITests.setUp
    call=test_cli.CLITests.call
    init=test_cli.CLITests.init

    def test_port_collision_uses_next_and_explicit_port_refuses(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1',0))
            port=occupied.getsockname()[1]
            if port==65535:self.skipTest('End of port range')
            self.assertNotEqual(ports.local_port(port),port)
            with self.assertRaises(Error):ports.local_port(port,1)
        state=self.init()
        self.assertGreaterEqual(state['port'],19471)
        self.assertEqual(config.read_env()['LVCT_PORT'],str(state['port']))

    def test_remote_port_probe_runs_on_remote_not_client(self):
        with patch.object(runtime,'run',return_value=subprocess.CompletedProcess([],0,'{"port":19472}','')) as run:
            self.assertEqual(ports.select(None,'ssh://owner@example.com:2222'),19472)
        command=run.call_args.args[0]
        self.assertEqual(command[0],'ssh')
        self.assertIn('owner@example.com',command)
        self.assertNotIn('StrictHostKeyChecking=no',repr(command))

    def test_error_groups_filters_and_multiline_traceback(self):
        lines=['app | ERROR IPC timeout -10005','Traceback (most recent call last):','  File "worker.py", line 5','ConnectionError: failed','app | INFO ready','app | CRITICAL No space left on device']
        result=diagnostics.errors_from_lines(lines)
        self.assertEqual(result['error_count'],2)
        self.assertEqual([x['code'] for x in result['groups']],['mt5_ipc','storage'])
        self.assertIn('ConnectionError',result['groups'][0]['sample'])
        self.assertEqual(len(diagnostics.filtered(lines,'critical')),1)
        follow=diagnostics.stream_filter('error')
        self.assertIsNone(follow('INFO healthy'))
        self.assertIn('ERROR',follow('ERROR broken'))
        self.assertEqual(follow('Traceback (most recent call last):'),'Traceback (most recent call last):')
        self.assertIsNone(follow('INFO healthy'))

    def test_health_uses_boolean_readiness_and_never_trade_calls(self):
        state=self.init()
        rows=[{'Service':x,'State':'running','Health':'healthy'} for x in ('app','mt5-sidecar')]
        probe={'app':True,'mt5':{'ok':False},'telegram':{'ok':True},'provider':{'ok':False}}
        with patch.object(runtime,'services',return_value=rows),patch.object(runtime,'execute',return_value=subprocess.CompletedProcess([],0,json.dumps(probe),'')) as execute:
            value=diagnostics.health(state)
        self.assertEqual(value['status'],'degraded')
        self.assertEqual(next(x for x in value['checks'] if x['name']=='mt5')['state'],'warning')
        self.assertEqual(execute.call_args.args[1:4],('exec','-T','app'))
        self.assertNotIn('order_send',diagnostics.HEALTH_PROBE)
        self.assertIn("data.get('status')",diagnostics.HEALTH_PROBE)

    def test_private_reports_never_send_automatically_and_scrub_known_and_generic_secrets(self):
        self.init()
        secret=config.read_env()['SECRET_KEY']
        raw='ERROR token=private-secret user@example.com 192.0.2.8 /home/person/private.db account 123456789 '+secret
        with patch.object(diagnostics,'health',return_value={'status':'unhealthy','checks':[]}),patch.object(diagnostics,'read_logs',return_value=[raw]),patch.object(reports,'send') as send:
            code,out=self.call('report','--note','failure')
            self.assertEqual(code,0,out)
            send.assert_not_called()
        file=next((self.home/'reports').glob('*.json'))
        value=reports.validate(json.loads(file.read_text()))
        for item in (secret,'private-secret','user@example.com','192.0.2.8','/home/person','123456789'):
            self.assertNotIn(item,json.dumps(value))
        with patch.object(reports,'send') as send:
            code,out=self.call('report','--send','--file',str(file))
            self.assertEqual(code,1,out)
            send.assert_not_called()
            code,out=self.call('report','--send','--yes','--file',str(file))
            self.assertEqual(code,0,out)
            send.assert_called_once()
        value['environment']={'SECRET_KEY':'bad'}
        with self.assertRaises(ValueError):reports.validate(value)

    def test_report_available_before_configuration(self):
        code,out=self.call('report')
        self.assertEqual(code,0,out)
        self.assertEqual(json.loads(next((self.home/'reports').glob('*.json')).read_text())['health']['status'],'unknown')

    def test_update_running_refuses_and_interrupted_update_blocks_start(self):
        state=self.init()
        with patch.object(runtime,'check_engine'),patch.object(runtime,'services',return_value=[{'State':'running'}]),patch.object(runtime,'stream') as stream:
            with self.assertRaisesRegex(Error,'stopped'):updates.apply(state)
            stream.assert_not_called()
        config.secure_write(self.home/'maintenance.json','{}')
        with patch.object(runtime,'check_engine') as check:
            code,out=self.call('start')
            self.assertEqual(code,1)
            check.assert_not_called()

    def test_offline_update_and_rollback_preserve_data_and_never_start(self):
        state=self.init()
        source=Path(self.temp.name)/'candidate'
        source.mkdir()
        for name in ('VERSION','docker-compose.yml','.env.example'):
            shutil.copy2(ROOT/name,source/name)
        shutil.copytree(ROOT/'cli',source/'cli',ignore=shutil.ignore_patterns('__pycache__','tests'))
        original=config.env_path().read_bytes()
        (self.home/'bin').mkdir()
        (self.home/'bin/lvct.pyz').write_bytes(b'old-cli')
        result=subprocess.CompletedProcess([],0,'sha256:'+'a'*64+'\n','')
        with patch.object(runtime,'check_engine'),patch.object(runtime,'services',return_value=[]),patch.object(runtime,'execute',return_value=result) as execute,patch.object(runtime,'run',return_value=result),patch.object(runtime,'stream',return_value=0) as stream,contextlib.redirect_stdout(io.StringIO()):
            updates.apply(state,source=source)
            self.assertNotEqual(config.load()['source'],state['source'])
            self.assertEqual(config.env_path().read_bytes(),original)
            self.assertFalse((self.home/'maintenance.json').exists())
            self.assertIn('build',stream.call_args.args[1])
            self.assertFalse(any('up' in call.args or 'start' in call.args for call in execute.call_args_list))
            updates.rollback(config.load())
        self.assertEqual(config.env_path().read_bytes(),original)
        self.assertEqual(config.load()['source'],state['source'])
        self.assertEqual((self.home/'bin/lvct.pyz').read_bytes(),b'old-cli')

    def test_mutation_lock_prevents_parallel_lifecycle_actions(self):
        from lvct.operations import lock
        with lock():
            with self.assertRaisesRegex(Error,'Another LVCT'):
                with lock():pass
        with lock():pass

    def test_failed_update_keeps_recovery_journal_and_old_env(self):
        state=self.init()
        source=Path(self.temp.name)/'candidate-failed'
        source.mkdir()
        for name in ('VERSION','docker-compose.yml','.env.example'):
            shutil.copy2(ROOT/name,source/name)
        shutil.copytree(ROOT/'cli',source/'cli',ignore=shutil.ignore_patterns('__pycache__','tests'))
        original=config.env_path().read_bytes()
        result=subprocess.CompletedProcess([],0,'sha256:'+'b'*64+'\n','')
        with patch.object(runtime,'check_engine'),patch.object(runtime,'services',return_value=[]),patch.object(runtime,'execute',return_value=result) as execute,patch.object(runtime,'run',return_value=result),patch.object(runtime,'stream',return_value=1),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(Error,'Build failed'):updates.apply(state,source=source)
            self.assertTrue((self.home/'maintenance.json').exists())
            self.assertEqual(config.load()['source'],state['source'])
            self.assertEqual(config.env_path().read_bytes(),original)
            updates.rollback(state)
        self.assertFalse((self.home/'maintenance.json').exists())
        self.assertFalse(any('up' in call.args or 'start' in call.args for call in execute.call_args_list))

    def test_report_trims_log_volume_to_upload_budget(self):
        state=self.init()
        lines=['ERROR '+('Ошибка '*300) for _ in range(100)]
        with patch.object(diagnostics,'health',return_value={'status':'unhealthy','checks':[]}),patch.object(diagnostics,'read_logs',return_value=lines):
            value=reports.collect(state)
        self.assertLess(len(json.dumps(value,ensure_ascii=False).encode()),55_001)
        self.assertTrue(value['logs'])
        self.assertLess(len(value['logs']),50)

    def test_errors_shows_local_failures_if_docker_is_unavailable(self):
        self.init()
        from lvct.events import record
        record('start','Docker unavailable')
        with patch.object(diagnostics,'errors',side_effect=Error('No Docker')):
            code,out=self.call('errors','--json')
        self.assertEqual(code,1,out)
        value=json.loads(out)
        self.assertFalse(value['runtime_logs_available'])
        self.assertEqual(value['cli_errors'][0]['command'],'start')
