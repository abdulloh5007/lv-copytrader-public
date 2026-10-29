"""Explicit, stopped-runtime updates with a durable recovery journal."""
from __future__ import annotations
import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import quote
from urllib.error import HTTPError

from . import config, runtime
from .console import Error, say

REPOSITORY = 'abdulloh5007/lv-copytrader-public'


def bootstrap(state):
    path = Path(state['source']) / 'cli/bootstrap.py'
    if not path.is_file():
        raise Error('The trusted local bootstrap is missing. Restore the installed source bundle.')
    spec = importlib.util.spec_from_file_location('lvct_update_bootstrap', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def metadata(state, ref='main'):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,199}', ref):
        raise Error('Invalid public revision.')
    value = json.loads(bootstrap(state).fetch('https://api.github.com/repos/' + REPOSITORY + '/commits/' + quote(ref, safe=''), limit=2*1024*1024))
    sha = value.get('sha', '')
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise Error('GitHub did not return an immutable public commit.')
    try:
        manifest=json.loads(bootstrap(state).fetch('https://api.github.com/repos/'+REPOSITORY+'/contents/cli/lvct/updates.py?ref='+sha,limit=256*1024))
        compatible=manifest.get('type')=='file' and manifest.get('path')=='cli/lvct/updates.py'
    except HTTPError as exc:
        if exc.code!=404:raise Error('Cannot check public update compatibility; retry later.') from exc
        compatible=False
    return {'schema':1,'available_revision':sha, 'installed_revision':state.get('revision','local'),
            'compatible':compatible,'update_available':compatible and sha != state.get('revision')}


def stopped(state):
    if any(x.get('State') in ('running','restarting','paused') for x in runtime.services(state)):
        raise Error('Update requires stopped services. Review positions, explicitly run lvct stop, then update --apply. No service was stopped automatically.')


def guard_start():
    if (config.home()/'maintenance.json').exists():
        raise Error('An interrupted update requires lvct update --rollback before starting.')


def apply(state, *, ref='main', source=None):
    guard_start()
    runtime.checked_env(state)
    runtime.check_engine(state)
    stopped(state)
    helper = bootstrap(state)
    with tempfile.TemporaryDirectory(prefix='lvct-update-') as tmp:
        work = Path(tmp)
        candidate = work/'source'
        candidate.mkdir()
        if source:
            revision = helper.local_source(Path(source).resolve(),candidate)
        else:
            revision = metadata(state,ref)['available_revision']
            archive = work/'source.zip'
            say('  Downloading public commit '+revision)
            helper.fetch('https://codeload.github.com/'+REPOSITORY+'/zip/'+revision,archive)
            helper.unpack(archive,candidate)
        if not all((candidate/p).is_file() for p in ('cli/bootstrap.py','cli/lvct/cli.py','cli/lvct/updates.py','docker-compose.yml','VERSION')):
            raise Error('This public revision does not include the CLI/runtime. Publish the new source before updating.')
        # Validate Python syntax without importing or running candidate code.
        for file in (candidate/'cli/lvct').glob('*.py'):
            compile(file.read_text(encoding='utf-8'),str(file),'exec')
        target = config.home()/'releases'/revision
        if target.is_symlink() or target.parent.is_symlink():
            raise Error('Symlinked release paths are refused.')
        if target.exists():
            for file in candidate.rglob('*'):
                if file.is_file():
                    cached=target/file.relative_to(candidate)
                    if cached.is_symlink() or any(parent.is_symlink() for parent in cached.parents if parent!=target.parent) or not cached.is_file() or cached.read_bytes()!=file.read_bytes():
                        raise Error('Existing immutable source differs; refusing update.')
        else:
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copytree(candidate,target)
        new={**state,'source':str(target),'revision':revision}
        # Compose validation does not print the rendered environment.
        runtime.execute(new,'config','--quiet',timeout=30)
        backup=config.home()/'updates'/str(time.time_ns())
        backup.mkdir(parents=True,mode=0o700)
        images={}
        for suffix in ('app','mt5'):
            tag=state['project']+'-'+suffix+':local'
            result=runtime.run(runtime.docker(state,'image','inspect','--format','{{.Id}}',tag),check=False)
            if result.returncode==0:
                image=result.stdout.strip()
                if not re.fullmatch(r'sha256:[0-9a-f]{64}',image):raise Error('Invalid previous image identity.')
                keep=state['project']+'-'+suffix+':rollback-'+backup.name
                runtime.run(runtime.docker(state,'image','tag',image,keep))
                images[tag]=keep
        for name in ('installation.json','bootstrap.json','bin/lvct.pyz'):
            original=config.home()/name
            if original.is_file():
                copied=backup/name
                copied.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(original,copied)
        journal={'schema':1,'backup':str(backup),'previous':state,'candidate':new,'images':images}
        config.secure_write(backup/'journal.json',json.dumps(journal))
        config.secure_write(config.home()/'maintenance.json',json.dumps(journal))
        say('  Building update. A first Wine build can exceed 40 minutes; services remain stopped.')
        if runtime.stream(new,runtime.compose(new,'build')):
            raise Error('Build failed. Run lvct update --rollback; no services were started.')
        runtime.execute(new,'create','--force-recreate','--no-build',timeout=None)
        # Use the installed trusted bootstrap, never an installer downloaded separately.
        with redirect_stdout(io.StringIO()):
            helper.install(argparse.Namespace(source=target,home=config.home(),ref='main',no_path=True,bin_dir=None))
        config.secure_write(config.home()/'installation.json',json.dumps(new,indent=2)+'\n')
        config.secure_write(config.home()/'last-update.json',json.dumps(journal))
        (config.home()/'maintenance.json').unlink()
        say('  Updated to '+revision+'. Services remain stopped. Run lvct doctor, then lvct start.', '32')


def rollback(state):
    path=config.home()/'maintenance.json'
    if not path.exists():path=config.home()/'last-update.json'
    if not path.exists():raise Error('There is no update to roll back.')
    journal=config.read_json(path)
    old=journal['previous']
    stopped(old)
    backup=Path(journal['backup'])
    if backup.parent!=config.home()/'updates' or backup.is_symlink():raise Error('Invalid update backup.')
    config.secure_write(config.home()/'maintenance.json',json.dumps(journal))
    for tag,keep in journal['images'].items():
        if not tag.startswith(old['project']+'-') or not keep.startswith(old['project']+'-'):
            raise Error('Invalid rollback image tag.')
        runtime.run(runtime.docker(old,'image','tag',keep,tag))
    if len(journal['images'])==2:
        runtime.execute(old,'create','--force-recreate','--no-build',timeout=None)
    else:
        # A first-build update has no old image to recreate. Remove stopped containers only.
        runtime.execute(journal['candidate'],'rm','--force',timeout=None)
    for name in ('installation.json','bootstrap.json','bin/lvct.pyz'):
        original=backup/name
        if original.is_file():
            destination=config.home()/name
            if destination.is_symlink():raise Error('Symlinked rollback destination.')
            temporary=destination.with_suffix('.rollback')
            if temporary.is_symlink():raise Error('Symlinked rollback temporary path.')
            shutil.copy2(original,temporary)
            temporary.replace(destination)
    (config.home()/'maintenance.json').unlink()
    last=config.home()/'last-update.json'
    if last.exists():last.unlink()
    say('  Previous source and images restored. Volumes were retained. Services remain stopped.', '32')
