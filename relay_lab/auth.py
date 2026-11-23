"""Console login: JWT in an HttpOnly cookie, never in the JSON body."""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Request, Response
from pydantic import BaseModel, Field

COOKIE_NAME = 'relay_lab_session'
TOKEN_TTL = timedelta(hours=12)
ALGORITHM = 'HS256'

_DEFAULT_USER = 'admin'
_DEFAULT_PASSWORD = 'relay-lab'
_LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})

LOGIN_WINDOW_SECONDS = 60
LOGIN_MAX_FAILURES = 8

_secret: str | None = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class LoginRateLimit(Exception):
    """Too many failed logins from one client."""


def configured_username() -> str:
    return os.environ.get('RELAY_LAB_USERNAME', _DEFAULT_USER).strip() or _DEFAULT_USER


def configured_password() -> str:
    # Keep trailing spaces if the operator set them deliberately via quotes in a shell;
    # only strip accidental surrounding whitespace from env assignment.
    value = os.environ.get('RELAY_LAB_PASSWORD', _DEFAULT_PASSWORD)
    return value.strip() if value is not None else _DEFAULT_PASSWORD


def uses_default_password() -> bool:
    return configured_password() == _DEFAULT_PASSWORD


def is_loopback_host(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK_HOSTS


def assert_bind_allowed(host: str) -> None:
    """Refuse non-loopback binds while the default password is still in use."""
    if is_loopback_host(host):
        return
    if uses_default_password():
        raise SystemExit(
            '拒绝以默认口令绑定非本机地址。'
            '请设置环境变量 RELAY_LAB_PASSWORD，或改用 --host 127.0.0.1。'
        )


def jwt_secret() -> str:
    """Stable for the process; override with RELAY_LAB_JWT_SECRET across restarts."""
    global _secret
    override = os.environ.get('RELAY_LAB_JWT_SECRET', '').strip()
    if override:
        return override
    if _secret is None:
        _secret = secrets.token_urlsafe(48)
    return _secret


def _fingerprint(value: str) -> bytes:
    """Hash before compare_digest so unequal string lengths never raise."""
    return hashlib.sha256(value.encode('utf-8')).digest()


def verify_credentials(username: str, password: str) -> bool:
    expected_user = configured_username()
    expected_pass = configured_password()
    user_ok = secrets.compare_digest(_fingerprint(username.strip()), _fingerprint(expected_user))
    pass_ok = secrets.compare_digest(_fingerprint(password), _fingerprint(expected_pass))
    return user_ok and pass_ok


class LoginGate:
    """Process-local failed-login throttle keyed by client address."""

    def __init__(self, *, limit: int = LOGIN_MAX_FAILURES, window: float = LOGIN_WINDOW_SECONDS) -> None:
        self.limit = limit
        self.window = window
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, stamps: list[float], now: float) -> list[float]:
        cutoff = now - self.window
        return [stamp for stamp in stamps if stamp >= cutoff]

    def check(self, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            stamps = self._prune(self._failures.get(client, []), now)
            self._failures[client] = stamps
            if len(stamps) >= self.limit:
                raise LoginRateLimit('登录失败次数过多，请稍后再试')

    def fail(self, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            stamps = self._prune(self._failures.get(client, []), now)
            stamps.append(now)
            self._failures[client] = stamps

    def succeed(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


LOGIN_GATE = LoginGate()


def client_address(request: Request) -> str:
    forwarded = request.headers.get('x-forwarded-for', '').split(',')[0].strip()
    if forwarded and os.environ.get('RELAY_LAB_TRUST_PROXY', '').strip() in {'1', 'true', 'yes'}:
        return forwarded
    if request.client and request.client.host:
        return request.client.host
    return 'unknown'


def issue_token(username: str) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        'sub': username,
        'iat': now,
        'exp': now + TOKEN_TTL,
    }
    return jwt.encode(payload, jwt_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, jwt_secret(), algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    subject = payload.get('sub')
    return subject if isinstance(subject, str) and subject else None


def user_from_request(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return decode_token(token)


def user_from_cookies(cookies: dict[str, str]) -> str | None:
    token = cookies.get(COOKIE_NAME)
    if not token:
        return None
    return decode_token(token)


def set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=int(TOKEN_TTL.total_seconds()),
        httponly=True,
        samesite='lax',
        path='/',
        secure=secure,
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        key=COOKIE_NAME,
        path='/',
        httponly=True,
        samesite='lax',
        secure=secure,
    )


def request_is_https(request: Request) -> bool:
    if request.url.scheme == 'https':
        return True
    if os.environ.get('RELAY_LAB_TRUST_PROXY', '').strip() not in {'1', 'true', 'yes'}:
        return False
    forwarded = request.headers.get('x-forwarded-proto', '')
    return forwarded.split(',')[0].strip().lower() == 'https'


PUBLIC_API_PATHS = frozenset({
    '/api/health',
    '/api/auth/login',
    '/api/auth/logout',
    '/api/auth/me',
})
