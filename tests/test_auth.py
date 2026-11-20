"""JWT cookie login: token stays in Set-Cookie, never in the JSON body."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay_lab.api import create_app
from relay_lab.auth import (
    COOKIE_NAME,
    LOGIN_GATE,
    LOGIN_MAX_FAILURES,
    assert_bind_allowed,
    configured_password,
    configured_username,
    verify_credentials,
)
from tests.conftest import login


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path, web_dir=tmp_path / 'missing', schedule_loop=False))


@pytest.fixture(autouse=True)
def _clear_login_gate() -> None:
    LOGIN_GATE.succeed('testclient')
    LOGIN_GATE.succeed('unknown')
    yield
    LOGIN_GATE.succeed('testclient')
    LOGIN_GATE.succeed('unknown')


def test_login_sets_httponly_cookie_without_token_in_body(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        '/api/auth/login',
        json={'username': configured_username(), 'password': configured_password()},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {'ok': True, 'user': configured_username()}
    assert 'token' not in body
    assert 'access_token' not in body
    assert 'jwt' not in body
    assert COOKIE_NAME in response.cookies
    set_cookie = response.headers.get('set-cookie', '')
    assert 'HttpOnly' in set_cookie or 'httponly' in set_cookie.lower()
    assert COOKIE_NAME in set_cookie


def test_login_rejects_bad_password(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        '/api/auth/login',
        json={'username': configured_username(), 'password': 'wrong-password'},
    )
    assert response.status_code == 401
    assert 'error' in response.json()
    assert COOKIE_NAME not in response.cookies


def test_login_rejects_unequal_length_password_without_500(tmp_path: Path) -> None:
    assert verify_credentials(configured_username(), 'x') is False
    client = _client(tmp_path)
    response = client.post(
        '/api/auth/login',
        json={'username': configured_username(), 'password': 'x'},
    )
    assert response.status_code == 401
    assert response.json()['error'] == '用户名或密码错误'


def test_login_rate_limit_after_repeated_failures(tmp_path: Path) -> None:
    client = _client(tmp_path)
    for _ in range(LOGIN_MAX_FAILURES):
        assert client.post(
            '/api/auth/login',
            json={'username': configured_username(), 'password': 'nope'},
        ).status_code == 401
    blocked = client.post(
        '/api/auth/login',
        json={'username': configured_username(), 'password': configured_password()},
    )
    assert blocked.status_code == 429


def test_default_password_cannot_bind_non_loopback() -> None:
    with pytest.raises(SystemExit):
        assert_bind_allowed('0.0.0.0')
    assert_bind_allowed('127.0.0.1')


def test_protected_routes_require_session(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get('/api/runs').status_code == 401
    assert client.get('/api/presets').status_code == 401
    assert client.get('/api/docs').status_code == 401
    assert client.get('/api/health').status_code == 200


def test_me_and_logout(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get('/api/auth/me').status_code == 401
    login(client)
    me = client.get('/api/auth/me')
    assert me.status_code == 200
    assert me.json() == {'user': configured_username()}
    assert client.get('/api/runs').status_code == 200

    logout = client.post('/api/auth/logout')
    assert logout.status_code == 200
    assert logout.json() == {'ok': True}
    assert 'token' not in logout.json()
    assert client.get('/api/auth/me').status_code == 401
    assert client.get('/api/runs').status_code == 401


def test_websocket_rejects_missing_session_with_4401(tmp_path: Path) -> None:
    """Accept-then-close so the browser sees 4401 instead of a forever-reconnect 1006."""
    from starlette.websockets import WebSocketDisconnect

    client = _client(tmp_path)
    with client.websocket_connect('/api/ws') as socket:
        with pytest.raises(WebSocketDisconnect) as raised:
            socket.receive_json()
    assert raised.value.code == 4401


def test_websocket_snapshot_requires_login(tmp_path: Path) -> None:
    client = _client(tmp_path)
    login(client)
    with client.websocket_connect('/api/ws') as socket:
        payload = socket.receive_json()
    assert payload['type'] == 'snapshot'
    assert isinstance(payload['runs'], list)
