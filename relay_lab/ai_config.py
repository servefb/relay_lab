"""Process-local configuration; only the operator can approve outbound destinations."""

from __future__ import annotations

import os
from threading import Lock
from typing import Literal
from urllib.parse import urlsplit

import httpx2
from pydantic import Field, SecretStr, ValidationError, field_validator

from .config import ConnectionSettings, Contract

AIProtocol = Literal['openai', 'openai_responses']
DEFAULT_URL = 'https://api.openai.com/v1'


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = httpx2.URL(url)
    return parsed.scheme, parsed.host, parsed.port


class AIConfig(Contract):
    url: str = Field(default=DEFAULT_URL, max_length=2048)
    model: str = Field(default='', max_length=200)
    protocol: AIProtocol = 'openai'
    timeout_seconds: int = Field(default=90, ge=2, le=180, strict=True)
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False, max_length=2048)

    @field_validator('url')
    @classmethod
    def base_url(cls, value: str) -> str:
        if '\\' in value or any(ord(ch) < 32 for ch in value):
            raise ValueError('AI 基址格式不合法')
        url = ConnectionSettings.validate_url(value)
        parts = urlsplit(url)
        parsed = httpx2.URL(url)
        if not parsed.host or '@' in parts.netloc or '?' in url or '#' in url:
            raise ValueError('AI 基址格式不合法')
        if parts.path.lower().endswith(('/chat/completions', '/responses', '/messages')):
            raise ValueError('请输入 SDK 基址，不要包含生成接口路径')
        return str(parsed).rstrip('/')

    @field_validator('model')
    @classmethod
    def strip_model(cls, value: str) -> str:
        return value.strip()

    @field_validator('api_key')
    @classmethod
    def normalize_key(cls, value: SecretStr | None) -> SecretStr | None:
        raw = value.get_secret_value().strip() if value else ''
        if raw and (not raw.isascii() or any(ord(ch) <= 32 or ord(ch) == 127 for ch in raw)):
            raise ValueError('AI Key 格式不合法')
        return SecretStr(raw) if raw else None

    @property
    def configured(self) -> bool:
        return bool(self.model and self.api_key)


class AIConfigUpdate(Contract):
    url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=200)
    protocol: AIProtocol | None = None
    timeout_seconds: int | None = Field(default=None, ge=2, le=180, strict=True)
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False, max_length=2048)


class AIConfigResponse(Contract):
    url: str
    model: str
    protocol: AIProtocol
    timeout_seconds: int
    has_key: bool
    configured: bool
    config_error: str | None = None


class AIConfigState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._allowed = {_origin(DEFAULT_URL)}
        self._error: str | None = None
        boot_url = os.getenv('RELAY_LAB_AI_URL', DEFAULT_URL)
        self._config = AIConfig()
        try:
            for url in [boot_url, *os.getenv('RELAY_LAB_AI_ALLOWED_ORIGINS', '').split(',')]:
                if url.strip():
                    self._allowed.add(_origin(AIConfig.base_url(url.strip())))
            self._config = AIConfig(
                url=boot_url,
                model=os.getenv('RELAY_LAB_AI_MODEL', ''),
                protocol=os.getenv('RELAY_LAB_AI_PROTOCOL', 'openai'),
                api_key=os.getenv('RELAY_LAB_AI_API_KEY'),
            )
        except (ValueError, httpx2.InvalidURL):
            self._error = 'AI 环境变量无效，请检查基址、协议和 Key'

    def snapshot(self) -> AIConfig:
        with self._lock:
            return self._config.model_copy()

    def _public(self) -> AIConfigResponse:
        config = self._config
        return AIConfigResponse(
            **config.model_dump(), has_key=bool(config.api_key),
            configured=config.configured, config_error=self._error,
        )

    def public(self) -> AIConfigResponse:
        with self._lock:
            return self._public()

    def update(self, patch: AIConfigUpdate) -> AIConfigResponse:
        with self._lock:
            values = self._config.model_dump()
            for field in patch.model_fields_set - {'api_key'}:
                value = getattr(patch, field)
                if value is None:
                    raise ValueError(f'{field} 不接受 null')
                values[field] = value
            try:
                url = AIConfig.base_url(values['url'])
                origin = _origin(url)
                # Logged-in PUT is operator approval: allow this origin for the process lifetime.
                # Boot-time RELAY_LAB_AI_ALLOWED_ORIGINS still seeds the set; unauthenticated
                # callers never reach this method.
                if origin not in self._allowed:
                    self._allowed.add(origin)
                values['url'] = url
                key_changed = 'api_key' in patch.model_fields_set
                values['api_key'] = patch.api_key if key_changed else (
                    self._config.api_key if url == self._config.url else None
                )
                self._config = AIConfig.model_validate(values)
            except ValueError:
                raise
            except (ValidationError, httpx2.InvalidURL) as exc:
                raise ValueError('AI 配置格式不合法') from exc
            self._error = None
            return self._public()
