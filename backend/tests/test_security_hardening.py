"""Adversarial auth regressions on isolated ASGI apps; no broker/network calls."""
import base64
import os
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.db import init_db
from app.main import create_app
from app.util.auth import COOKIE_NAME, LoginRateLimiter, MAX_FAILED_ATTEMPTS

USER, PASSWORD = 'owner', 'unique-test-owner-password'

@pytest.fixture
async def hardened(data_dir, monkeypatch):
    monkeypatch.setattr(settings, 'SP_ALLOW_UNAUTHENTICATED', False)
    monkeypatch.setattr(settings, 'SP_AUTH_USER', USER)
    monkeypatch.setattr(settings, 'SP_AUTH_PASSWORD', PASSWORD)
    await init_db()
    return create_app()


def client(app, peer='198.51.100.1'):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 1234)), base_url='https://owner.test')


async def login(c):
    r = await c.post('/api/auth/login', json={'username': USER, 'password': PASSWORD})
    assert r.status_code == 200
    return c.cookies[COOKIE_NAME]


@pytest.mark.parametrize('username,password', [('', ''), ('owner', ''), ('', PASSWORD)])
async def test_missing_or_partial_credentials_never_open_private_api(data_dir, monkeypatch, username, password):
    monkeypatch.setattr(settings, 'SP_ALLOW_UNAUTHENTICATED', False)
    monkeypatch.setattr(settings, 'SP_AUTH_USER', username)
    monkeypatch.setattr(settings, 'SP_AUTH_PASSWORD', password)
    app = create_app()
    async with client(app) as c:
        auth = 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()
        assert (await c.get('/api/waitlist', headers={'Authorization': auth})).status_code == 401
        assert (await c.get('/api/auth/status')).json() == {'authenticated': False, 'required': True, 'username': None}


async def test_logout_revokes_stolen_copy_and_survives_restart(hardened):
    async with client(hardened) as c:
        token = await login(c)
        assert (await c.get('/api/waitlist')).status_code == 200
        assert (await c.post('/api/auth/logout')).status_code == 200
    for app in [hardened, create_app()]:
        async with client(app) as attacker:
            r = await attacker.get('/api/waitlist', headers={'Cookie': COOKIE_NAME + '=' + token})
            assert r.status_code == 401


async def test_same_second_sessions_are_unique_and_logout_is_per_session(hardened):
    async with client(hardened) as first, client(hardened) as second:
        a, b = await login(first), await login(second)
        assert a != b
        await first.post('/api/auth/logout')
        assert (await second.get('/api/waitlist')).status_code == 200


async def test_password_rotation_invalidates_old_cookie(hardened, monkeypatch):
    async with client(hardened) as c:
        token = await login(c)
    monkeypatch.setattr(settings, 'SP_AUTH_PASSWORD', 'replacement-owner-password')
    async with client(create_app()) as c:
        assert (await c.get('/api/waitlist', headers={'Cookie': COOKIE_NAME + '=' + token})).status_code == 401


async def test_ip_spoof_does_not_bypass_login_limit_and_restart_does_not_reset_it(hardened):
    async with client(hardened) as c:
        for i in range(MAX_FAILED_ATTEMPTS):
            r = await c.post('/api/auth/login', json={'username': USER, 'password': 'wrong'},
                             headers={'X-Forwarded-For': f'203.0.113.{i}', 'CF-Connecting-IP': f'203.0.113.{i}'})
            assert r.status_code == 401
    async with client(create_app()) as c:
        r = await c.post('/api/auth/login', json={'username': USER, 'password': 'wrong'})
        assert r.status_code == 429 and int(r.headers['retry-after']) > 0
    raw = (Path(settings.DATA_DIR) / 'auth-rate.json').read_text()
    assert '198.51.100.1' not in raw and PASSWORD not in raw
    assert os.stat(Path(settings.DATA_DIR) / 'auth-rate.json').st_mode & 0o777 == 0o600


async def test_distributed_guessing_is_bounded_without_breaking_valid_basic_health(hardened):
    hardened.state.login_rate_limiter.global_max_attempts = 4
    for i in range(4):
        async with client(hardened, f'198.51.100.{i}') as c:
            assert (await c.post('/api/auth/login', json={'username': USER, 'password': 'wrong'})).status_code == 401
    async with client(hardened, '203.0.113.50') as c:
        assert (await c.post('/api/auth/login', json={'username': USER, 'password': 'wrong'})).status_code == 429
        assert (await c.get('/api/waitlist', auth=(USER, PASSWORD))).status_code == 200


async def test_origin_scheme_mismatch_is_rejected(hardened):
    async with client(hardened) as c:
        response = await c.post('/api/auth/login', json={'username': USER, 'password': PASSWORD},
                                headers={'Origin': 'http://owner.test'})
        assert response.status_code == 403


async def test_private_chunked_requests_are_bounded(hardened):
    async def body():
        for _ in range(17):
            yield b'a' * 65536
    async with client(hardened) as c:
        response = await c.post('/api/settings/notifier/test', content=body(), headers={'Content-Type': 'application/json'})
        assert response.status_code == 413


async def test_host_allowlist_rejects_dns_rebinding_host(hardened, monkeypatch):
    monkeypatch.setattr(settings, 'SP_ALLOWED_HOSTS', 'owner.test,127.0.0.1,localhost')
    async with client(create_app()) as c:
        assert (await c.get('/api/auth/status', headers={'Host': 'attacker.invalid'})).status_code == 400
        assert (await c.get('/api/auth/status')).status_code == 200


async def test_static_symlink_never_exposes_owner_file(hardened, monkeypatch, tmp_path):
    dist = tmp_path / 'dist'
    (dist / 'assets').mkdir(parents=True)
    outside = tmp_path / 'owner-secret.txt'
    outside.write_text('OWNER-PRIVATE-NEVER-PUBLIC')
    (dist / 'assets' / 'leak.txt').symlink_to(outside)
    monkeypatch.setattr(settings, 'FRONTEND_DIST', str(dist))
    async with client(create_app()) as c:
        response = await c.get('/assets/leak.txt')
        assert response.status_code == 404
        assert 'OWNER-PRIVATE-NEVER-PUBLIC' not in response.text


def test_image_bomb_rejected_before_pixel_decode(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.util import images
    from app.errors import IngestError
    source = tmp_path / 'header-only.png'
    source.write_bytes(b'header')
    def must_not_load():
        pytest.fail('oversized pixels were decoded')
    fake = SimpleNamespace(width=50000, height=50000, load=must_not_load, close=lambda: None)
    monkeypatch.setattr(images.Image, 'open', lambda _: fake)
    with pytest.raises(IngestError, match='megapixel'):
        images._open_rgb(str(source))


def test_oversized_file_rejected_before_image_parser(tmp_path):
    from app.util import images
    from app.errors import IngestError
    source = tmp_path / 'oversized.png'
    with source.open('wb') as f:
        f.truncate(images.MAX_SOURCE_IMAGE_BYTES + 1)
    with pytest.raises(IngestError, match='20 MiB'):
        images._open_rgb(str(source))


async def test_streaming_telegram_image_limit_removes_partial_file(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.telegram import client as tg
    from app.util.images import MAX_SOURCE_IMAGE_BYTES
    from app.errors import IngestError
    async def download(message, *, file, progress_callback):
        Path(file).write_bytes(b'partial')
        progress_callback(MAX_SOURCE_IMAGE_BYTES + 1, None)
    service = tg.TelegramService(lambda: None, settings)
    monkeypatch.setattr(tg, '_has_downloadable_image', lambda _: True)
    monkeypatch.setattr(service, '_require_client', lambda: SimpleNamespace(download_media=download))
    monkeypatch.setattr(tg.tg_utils, 'get_extension', lambda _: '.png')
    with pytest.raises(IngestError, match='safety limit'):
        await service.download_image(SimpleNamespace(media=object()), str(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('token', ['ghp_' + 'A' * 36, 'gd_pat_' + 'B' * 50, 'AIza' + 'C' * 35])
def test_unregistered_cloud_tokens_are_never_logged(token):
    from app.util.logging import redact
    assert token not in redact('upstream accidentally echoed ' + token)


async def test_revoked_open_websocket_sends_no_more_private_frames(hardened):
    import time
    from app.util.auth import AuthMiddleware, _sign
    guard = hardened.state.auth_sessions
    token = _sign(guard.secret, int(time.time()))
    messages = []
    async def inner(scope, receive, send):
        await send({'type': 'websocket.accept'})
        guard.revoke(token)
        await send({'type': 'websocket.send', 'text': 'PRIVATE-AFTER-LOGOUT'})
    async def receive():
        return {'type': 'websocket.connect'}
    async def send(message):
        messages.append(message)
    app = AuthMiddleware(inner, username=USER, password=PASSWORD, secret=guard.secret, sessions=guard)
    await app({'type': 'websocket', 'path': '/api/stream', 'client': ('198.51.100.1', 1234),
               'headers': [(b'cookie', (COOKIE_NAME + '=' + token).encode())]}, receive, send)
    assert [m['type'] for m in messages] == ['websocket.accept', 'websocket.close']
    assert messages[-1]['code'] == 1008
