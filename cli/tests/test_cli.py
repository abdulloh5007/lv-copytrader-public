"""CLI isolation and installer regressions. Docker calls are mocked; no broker is contacted."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

CLI = Path(__file__).resolve().parents[1]
ROOT = CLI.parent
sys.path.insert(0, str(CLI))
from lvct import cli, config, runtime
from lvct.console import Error
spec = importlib.util.spec_from_file_location('lvct_bootstrap', CLI / 'bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'state with spaces'
        self.env = patch.dict(os.environ, {'LVCT_HOME': str(self.home)})
        context=patch.object(runtime,'context_host',return_value='unix:///var/run/docker.sock')
        context.start()
        self.addCleanup(context.stop)
        self.env.start()
        self.addCleanup(self.env.stop)

    def call(self, *args, stdin=''):
        out = io.StringIO()
        with patch('sys.stdin', io.StringIO(stdin)), contextlib.redirect_stdout(out):
            code = cli.main(list(args))
        return code, out.getvalue()

    def init(self, password='unit-admin-password-long', **kwargs):
        data = {'admin_user': 'operator', 'admin_password': password, 'mt5_server': 'Example-Demo',
                'mt5_login': '123456', 'mt5_password': 'test-broker-password', **kwargs}
        code, out = self.call('init', '--source', str(ROOT), '--context', 'default', '--from-stdin', stdin=json.dumps(data))
        self.assertEqual(code, 0, out)
        return config.load()

    def test_noninteractive_menu_is_help_without_side_effects(self):
        code, out = self.call()
        self.assertEqual(code, 0)
        self.assertIn('password', out)
        self.assertFalse(self.home.exists())

    def test_setup_is_private_and_does_not_start_or_overwrite(self):
        with patch.object(runtime, 'run') as run:
            state = self.init()
            run.assert_not_called()
        env = config.read_env()
        self.assertEqual(env['SP_ALLOW_UNAUTHENTICATED'], 'false')
        self.assertEqual(len(__import__('base64').urlsafe_b64decode(env['SECRET_KEY'])), 32)
        self.assertEqual(runtime.volume_names(state), [state['project'] + x for x in ('_data', '_mt5', '_x11')])
        original = config.env_path().read_bytes()
        code, out = self.call('init', '--from-stdin')
        self.assertEqual(code, 1)
        self.assertEqual(config.env_path().read_bytes(), original)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(config.env_path().stat().st_mode), 0o600)

    def test_remote_setup_creates_its_context_without_switching_global_context(self):
        data = {'admin_user':'owner','admin_password':'remote-test-password-long','mt5_server':'Demo',
                'mt5_login':'1234','mt5_password':'test-broker-password'}
        with patch.object(runtime,'run',return_value=subprocess.CompletedProcess([],0,'','')) as run, patch('lvct.ports.select',return_value=19471):
            code,out = self.call('init','--source',str(ROOT),'--remote','ubuntu@server.example',
                                 '--from-stdin',stdin=json.dumps(data))
        self.assertEqual(code,0,out)
        command=run.call_args.args[0]
        self.assertEqual(command[:3],['docker','context','create'])
        self.assertIn('host=ssh://ubuntu@server.example:22',command)
        self.assertNotIn('use',command)
        self.assertNotIn(data['admin_password'],repr(run.call_args))

    def test_invalid_setup_leaves_no_partial_credentials(self):
        for changes in [{'admin_password': 'short'}, {'mt5_login': '12; echo'}, {'admin_user': 'bad:user'}, {'mt5_server': 'A\nB'}]:
            data = {'admin_user': 'op', 'admin_password': 'test-admin-password-long', 'mt5_login': '12', 'mt5_server': 'Demo', 'mt5_password': 'test', **changes}
            code, _ = self.call('init', '--source', str(ROOT), '--context', 'default', '--from-stdin', stdin=json.dumps(data))
            self.assertEqual(code, 1)
            self.assertFalse(config.env_path().exists())

    def test_environment_round_trip_and_redacted_config(self):
        password = "  pаss $HOME ${NAME} $$ # ' \\\" !  "
        self.init(password=password)
        self.assertEqual(config.read_env()['SP_AUTH_PASSWORD'], password)
        code, out = self.call('config')
        self.assertEqual(code, 0)
        self.assertNotIn(password, out)
        self.assertNotIn(json.dumps(password, ensure_ascii=False)[1:-1], out)
        self.assertNotIn('test-broker-password', out)
        self.assertNotIn(config.read_env()['SECRET_KEY'], out)

    def test_ambient_docker_and_secret_environment_cannot_override_installation(self):
        with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://wrong:2375', 'DOCKER_CONTEXT': 'wrong', 'COMPOSE_FILE': '/other', 'SP_AUTH_PASSWORD': 'ambient', 'HTTPS_PROXY': 'http://proxy'}):
            env = runtime.child_env({'SP_AUTH_PASSWORD': 'managed'})
        for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'COMPOSE_FILE', 'SP_AUTH_PASSWORD'):
            self.assertNotIn(key, env)
        self.assertEqual(env['HTTPS_PROXY'], 'http://proxy')

    def test_modified_volume_identity_is_refused_before_compose(self):
        state = self.init()
        values = config.read_env()
        values['LVCT_DATA_VOLUME'] = 'some-other-project-data'
        config.write_env(values)
        with patch.object(runtime, 'run') as run:
            with self.assertRaises(Error):runtime.execute(state, 'ps')
            run.assert_not_called()

    def test_start_on_running_installation_never_recreates_containers(self):
        self.init()
        with patch.object(runtime, 'check_engine'), patch.object(runtime, 'services', return_value=[{'Service': x, 'State': 'running'} for x in ('app', 'mt5-sidecar')]), patch.object(runtime, 'stream') as stream, patch.object(runtime, 'prepare_volumes') as volumes:
            code, _ = self.call('start', '--build')
            self.assertEqual(code, 0)
            stream.assert_not_called()
            volumes.assert_not_called()

    def test_start_is_isolated_and_does_not_recreate_partial_stack(self):
        state = self.init()
        with patch.object(runtime, 'check_engine'), patch.object(runtime, 'services', return_value=[]), patch.object(runtime, 'prepare_volumes'), patch.object(runtime, 'stream', return_value=0) as stream:
            code, _ = self.call('start')
        self.assertEqual(code, 0)
        command = stream.call_args.args[1]
        self.assertIn('--no-recreate', command)
        self.assertIn(state['project'], command)
        self.assertIn(str(config.env_path()), command)
        self.assertNotIn('down', command)

    def test_wrong_volume_ownership_blocks_start(self):
        state = self.init()
        result = subprocess.CompletedProcess([], 0, json.dumps([{'Labels': {'lvct.installation': 'someone-else'}}]), '')
        with patch.object(runtime, 'run', return_value=result) as run:
            with self.assertRaises(Error):runtime.prepare_volumes(state)
            self.assertEqual(run.call_count, 1)

    def test_volume_permission_failure_does_not_create_empty_replacement(self):
        state = self.init()
        with patch.object(runtime, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'permission denied')) as run:
            with self.assertRaises(Error):runtime.prepare_volumes(state)
            self.assertEqual(run.call_count, 1)

    def test_password_uses_stdin_not_argv_and_never_restarts(self):
        self.init()
        secret = 'new-local-password-long'
        with patch.object(runtime, 'services', return_value=[{'Service': 'app', 'State': 'running'}]), patch.object(runtime, 'execute', return_value=subprocess.CompletedProcess([], 0, 'Password updated', '')) as execute:
            code, out = self.call('password', '--stdin', stdin=secret + '\n')
        self.assertEqual(code, 0)
        self.assertNotIn(secret, repr(execute.call_args.args))
        self.assertEqual(json.loads(execute.call_args.kwargs['data'])['password'], secret)
        self.assertNotIn(secret, out)
        self.assertEqual(execute.call_count, 1)
        self.assertNotIn('restart', execute.call_args.args)

    def test_default_arm_setup_explains_remote_mode_before_writing(self):
        with patch('platform.machine', return_value='arm64'):
            code, out = self.call('init')
        self.assertEqual(code, 1)
        self.assertIn('--remote', out)
        self.assertFalse(config.env_path().exists())

    def test_arm_engine_rejected_before_any_start(self):
        for arch in ['arm64', 'aarch64']:
            with patch.object(runtime, 'ensure_endpoint'), patch.object(runtime, 'info', return_value={'OSType': 'linux', 'Architecture': arch}), patch.object(runtime, 'run') as run:
                with self.assertRaisesRegex(Error, 'remote'):runtime.check_engine({'context': 'default'}, display=False)
                run.assert_not_called()

    def test_windows_container_engine_rejected(self):
        with patch.object(runtime, 'ensure_endpoint'), patch.object(runtime, 'info', return_value={'OSType': 'windows', 'Architecture': 'x86_64'}):
            with self.assertRaisesRegex(Error, 'Linux containers'):runtime.check_engine({'context': 'default'})

    def test_stop_and_restart_require_interactive_acknowledgement(self):
        self.init()
        for action in ['stop', 'restart']:
            with patch.object(runtime, 'execute') as execute:
                code, _ = self.call(action)
                self.assertEqual(code, 1)
                execute.assert_not_called()

    def test_plain_tcp_context_cannot_receive_private_configuration(self):
        with patch.object(runtime, 'context_host', return_value='tcp://untrusted:2375'), patch.object(runtime, 'info') as info:
            with self.assertRaisesRegex(Error, 'TCP'):runtime.check_engine({'context':'default'})
            info.assert_not_called()

    def test_ssh_url_injection_and_passwords_are_rejected(self):
        for value in ['ssh://user:password@example.com', 'ssh://-bad@host', 'ssh://user@-oProxyCommand=evil', 'user@host/path', 'user@host?x=1']:
            with self.assertRaises(Error):config.ssh_target(value)
        self.assertEqual(config.ssh_target('ubuntu@example.com'), ('ubuntu', 'example.com', 22))

    def test_remote_panel_uses_loopback_tunnel(self):
        state = self.init()
        args = cli.parser().parse_args(['open', '--no-browser'])
        with patch.object(runtime, 'context_host', return_value='ssh://ubuntu@server.example:2222'), patch('subprocess.call', return_value=0) as call:
            self.assertEqual(cli._open(state, args), 0)
        command = call.call_args.args[0]
        self.assertIn(f"127.0.0.1:{state['port']}:127.0.0.1:{state['port']}", command)
        self.assertNotIn('StrictHostKeyChecking=no', command)
        self.assertIn('ubuntu@server.example', command)

    def test_os_dependency_plans(self):
        with patch('platform.system', return_value='Windows'):
            command, _ = runtime.dependency_plan()
            self.assertIn('Docker.DockerDesktop', command)
        with patch('platform.system', return_value='Darwin'), patch('shutil.which', return_value='/opt/homebrew/bin/brew'):
            command, _ = runtime.dependency_plan()
            self.assertEqual(command, ['brew', 'install', '--cask', 'docker-desktop'])

    @unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI is optional for unit tests')
    def test_real_compose_parser_preserves_passwords_and_isolated_paths(self):
        probe = subprocess.run(['docker', 'compose', 'version'], capture_output=True)
        if probe.returncode:
            self.skipTest('Docker Compose plugin is not installed')
        secret = "  cli-test $HOME ${SHOULD_NOT_EXPAND} $$ # ' \\\" пароль  "
        state = self.init(password=secret)
        result = runtime.execute(state, 'config', '--format', 'json')
        data = json.loads(result.stdout)
        # Compose config escapes dollars again for a round-trippable rendered file.
        self.assertEqual(data['services']['app']['environment']['SP_AUTH_PASSWORD'].replace('$$', '$'), secret)
        interpolation = runtime.execute(state, 'config', '--environment').stdout
        self.assertIn('SP_AUTH_PASSWORD=' + secret, interpolation)
        self.assertEqual(data['services']['mt5-sidecar']['environment']['MT5_PASSWORD'], 'test-broker-password')
        self.assertEqual(data['volumes']['lvCT-data']['name'], state['project'] + '_data')
        self.assertEqual(data['services']['app']['ports'][0]['host_ip'], '127.0.0.1')
        self.assertNotIn('ports', data['services']['mt5-sidecar'])


class InstallerTests(unittest.TestCase):
    def test_archive_rejects_escape_symlink_private_and_duplicate_paths(self):
        cases = [('root/../outside', False), ('root/CON.txt', False), ('root/space. ', False), ('root/.env', False), ('root/backend/key.pem', False), ('root/link', True)]
        for name, symlink in cases:
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / 'source.zip'
                with zipfile.ZipFile(archive, 'w') as z:
                    item = zipfile.ZipInfo(name)
                    if symlink:item.external_attr = (stat.S_IFLNK | 0o777) << 16
                    z.writestr(item, 'test')
                with self.assertRaises(ValueError):bootstrap.unpack(archive, Path(tmp) / 'out')
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'source.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('root/A.txt', 'one');z.writestr('root/a.txt', 'two')
            with self.assertRaises(ValueError):bootstrap.unpack(archive, Path(tmp) / 'out')

    def test_offline_zipapp_install_works_and_preserves_existing_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            for name in ['VERSION', 'docker-compose.yml', '.env.example']:
                (source / name).write_text('0.1.0\n' if name == 'VERSION' else '# fixture\n')
            shutil.copytree(CLI / 'lvct', source / 'cli/lvct', ignore=shutil.ignore_patterns('__pycache__'))
            (source / '.env').write_text('must-not-copy')
            (source / 'runtime-state').mkdir()
            (source / 'runtime-state/private.txt').write_text('must-not-copy')
            home = root / 'home проба'
            home.mkdir()
            original = '{"owner": "existing"}'
            (home / 'installation.json').write_text(original)
            args = argparse_namespace(source=source, home=home, no_path=True)
            with contextlib.redirect_stdout(io.StringIO()):bootstrap.install(args)
            self.assertEqual((home / 'installation.json').read_text(), original)
            with contextlib.redirect_stdout(io.StringIO()):bootstrap.install(args)
            self.assertEqual((home / 'installation.json').read_text(), original)
            bundle = Path(json.loads((home / 'bootstrap.json').read_text())['source'])
            self.assertFalse((bundle / '.env').exists())
            self.assertFalse((bundle / 'runtime-state').exists())
            result = subprocess.run([sys.executable, str(home / 'bin/lvct.pyz'), '--help'], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('password', result.stdout)
            (bundle/'VERSION').write_text('tampered')
            with self.assertRaises(ValueError):bootstrap.install(args)


def argparse_namespace(**values):
    from argparse import Namespace
    return Namespace(**{'ref':'main','source':None,'home':None,'bin_dir':None,'no_path':False, **values})


class DistributionTests(unittest.TestCase):
    def make_archive(self,root):
        archive=root/'source.zip'
        with zipfile.ZipFile(archive,'w') as package:
            for path in ['VERSION','docker-compose.yml','.env.example']:
                package.writestr('root/'+path,'0.1.0\n' if path=='VERSION' else '# fixture')
            for file in (CLI/'lvct').glob('*.py'):package.write(file,'root/cli/lvct/'+file.name)
        return archive

    def test_official_distribution_installs_verified_source_and_rejects_tampering(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive=self.make_archive(root)
            digest=hashlib.sha256(archive.read_bytes()).hexdigest()
            manifest={'schema':1,'size':archive.stat().st_size,'sha256':digest,'url':'https://docs.lvenc.site/releases/'+digest+'.zip','channel':'preview'}
            def fetch(url,target=None,**kwargs):
                if target:shutil.copy2(archive,target)
                else:return json.dumps(manifest).encode()
            args=argparse_namespace(home=root/'home',distribution='https://docs.lvenc.site/releases/current.json',no_path=True)
            with patch.object(bootstrap,'fetch',side_effect=fetch),contextlib.redirect_stdout(io.StringIO()):bootstrap.install(args)
            state=json.loads((args.home/'bootstrap.json').read_text())
            self.assertEqual(state['revision'],'download-'+digest)
            manifest['sha256']='0'*64;manifest['url']='https://docs.lvenc.site/releases/'+'0'*64+'.zip'
            args.home=root/'tampered'
            with patch.object(bootstrap,'fetch',side_effect=fetch),self.assertRaisesRegex(ValueError,'integrity'),contextlib.redirect_stdout(io.StringIO()):bootstrap.install(args)
            self.assertFalse((args.home/'bootstrap.json').exists())
            args.distribution='https://untrusted.example/manifest.json'
            with self.assertRaisesRegex(ValueError,'official'),contextlib.redirect_stdout(io.StringIO()):bootstrap.install(args)

if __name__ == '__main__':
    unittest.main()
