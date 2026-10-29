"""Password rotation is live, persistent and invalidates cookies; no trading lifespan runs."""
import io
import json
from pathlib import Path
import sys

import httpx
import pytest

from app.config import settings
from app.main import create_app
from app.cli import admin
from app.util.admin_credentials import AdminCredentials, hash_password, password_matches
from app.util.auth import COOKIE_NAME

OLD = 'old-test-password-long'
NEW = 'new-test-password-пароль'


def test_hash_is_salted_and_does_not_store_password():
    a, b = hash_password(NEW), hash_password(NEW)
    assert a != b and NEW not in a
    assert password_matches(NEW, a)
    assert not password_matches(OLD, a)
    assert not password_matches(a, a)


async def test_cli_rotation_applies_without_app_restart_and_revokes_old_access(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(settings, 'SP_AUTH_USER', 'owner')
    monkeypatch.setattr(settings, 'SP_AUTH_PASSWORD', OLD)
    monkeypatch.setattr(settings, 'SP_ALLOW_UNAUTHENTICATED', False)
    app = create_app()
    key = settings.SECRET_KEY
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as c:
        assert (await c.post('/api/auth/login', json={'username':'owner','password':OLD})).status_code == 200
        cookie = c.cookies[COOKIE_NAME]
        assert (await c.get('/api/docs')).status_code == 200
        monkeypatch.setattr(sys, 'argv', ['admin', 'password'])
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'password':NEW})))
        assert admin.main() == 0
        assert NEW not in capsys.readouterr().out
        assert settings.SECRET_KEY == key
        assert (await c.get('/api/docs')).status_code == 401
        c.cookies.clear()
        assert (await c.post('/api/auth/login', json={'username':'owner','password':OLD})).status_code == 401
        assert (await c.get('/api/docs', auth=('owner',OLD))).status_code == 401
        assert (await c.post('/api/auth/login', json={'username':'owner','password':NEW})).status_code == 200
        assert (await c.get('/api/docs')).status_code == 200
        assert (await c.get('/api/auth/status')).status_code == 200
    # A fresh app reads the same override and still rejects the previous cookie/password.
    restarted = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='http://local') as c:
        assert (await c.get('/api/docs', headers={'Cookie':COOKIE_NAME+'='+cookie})).status_code == 401
        assert (await c.post('/api/auth/login', json={'username':'owner','password':NEW})).status_code == 200
    state = (Path(settings.DATA_DIR) / 'admin-credentials.json').read_text()
    assert NEW not in state and OLD not in state


def test_invalid_override_fails_closed(tmp_path):
    path = tmp_path/'admin-credentials.json'
    store = AdminCredentials(path, lambda: ('owner',OLD))
    assert store.current() == ('owner',OLD)
    path.write_text('{"version":1,"username":"owner","password_hash":"bad"}')
    with pytest.raises(RuntimeError):store.current()
    with pytest.raises(RuntimeError):store.change(NEW)


def test_rotation_can_change_username_and_rejects_bad_input(tmp_path):
    store = AdminCredentials(tmp_path/'admin-credentials.json', lambda: ('owner',OLD))
    for password in ['short', 'long-enough-but\nnewline']:
        with pytest.raises(ValueError):store.change(password)
    assert not store.path.exists()
    store.change(NEW, 'new-owner')
    assert store.current()[0] == 'new-owner'
    assert password_matches(NEW,store.current()[1])


@pytest.mark.parametrize('method',['cookie','basic'])
async def test_rotation_closes_open_websocket_before_another_frame(tmp_path, method):
    import base64
    import time
    from app.util.auth import AuthMiddleware, _sign
    from app.util.auth_state import SessionGuard
    store = AdminCredentials(tmp_path/'admin-credentials.json', lambda: ('owner',OLD))
    sessions = SessionGuard(tmp_path/'sessions.json', lambda:'test-cookie-signing', store.current)
    if method == 'cookie':
        headers=[(b'cookie',(COOKIE_NAME+'='+_sign(sessions.secret,int(time.time()))).encode())]
    else:
        headers=[(b'authorization',b'Basic '+base64.b64encode(('owner:'+OLD).encode()))]
    messages=[]
    async def protected(scope, receive, send):
        await send({'type':'websocket.accept'})
        store.change(NEW)
        await send({'type':'websocket.send','text':'must-never-leak'})
    async def receive():return {'type':'websocket.connect'}
    async def send(message):messages.append(message)
    gate = AuthMiddleware(protected,username='owner',password=OLD,secret=lambda:sessions.secret,
                          sessions=sessions,credentials=store.current)
    await gate({'type':'websocket','path':'/api/stream','headers':headers,'client':('127.0.0.1',1234)},receive,send)
    assert [x['type'] for x in messages] == ['websocket.accept','websocket.close']
    assert messages[-1]['code'] == 1008


@pytest.mark.parametrize('method',['login','basic'])
async def test_inflight_old_password_cannot_mint_a_new_session_during_rotation(data_dir,monkeypatch,method):
    monkeypatch.setattr(settings,'SP_AUTH_USER','owner')
    monkeypatch.setattr(settings,'SP_AUTH_PASSWORD',OLD)
    monkeypatch.setattr(settings,'SP_ALLOW_UNAUTHENTICATED',False)
    app=create_app()
    store=app.state.admin_credentials
    async def rotate_while_verifying(*args):
        store.change(NEW)
        return True
    if method=='login':
        from app.api import auth as module
    else:
        from app.util import auth as module
    monkeypatch.setattr(module,'verify',rotate_while_verifying)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as c:
        if method=='login':
            response=await c.post('/api/auth/login',json={'username':'owner','password':OLD})
        else:
            response=await c.get('/api/docs',auth=('owner',OLD))
        assert response.status_code==401
        assert COOKIE_NAME not in response.cookies
