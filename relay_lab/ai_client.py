"""OpenAI SDK transport shared by report analysis and the read-only assistant."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio
import httpx2
import openai

from . import netutil
from .ai_config import AIConfig

MAX_OUTPUT_CHARS = 24_000
MAX_ARGUMENT_CHARS = 8_000


class AIError(Exception):
    def __init__(self, code: str, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class ToolCall:
    id: str = ''
    name: str = ''
    arguments: str = ''


class KeyFilter:
    """Keep a partial key suffix private until it matches or is ruled out."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.pending = ''

    def feed(self, text: str, *, final: bool = False) -> str:
        if not self.key:
            return text
        pending = (self.pending + text).replace(self.key, '[已脱敏]')
        length = 0 if final else next(
            (i for i in range(min(len(pending), len(self.key) - 1), 0, -1) if pending.endswith(self.key[:i])), 0,
        )
        self.pending = pending[-length:] if length else ''
        return pending[:-length] if length else pending


def redact_key(value: Any, key: str) -> Any:
    if isinstance(value, str):
        return value.replace(key, '[已脱敏]') if key else value
    if isinstance(value, list):
        return [redact_key(item, key) for item in value]
    if isinstance(value, dict):
        return {name: redact_key(item, key) for name, item in value.items()}
    return value


def _bad_stream() -> AIError:
    return AIError('upstream_incomplete', '上游未返回完整的生成结果')


def _transport() -> httpx2.AsyncClient:
    netutil.sanitize_proxy_environment()
    try:
        return httpx2.AsyncClient(trust_env=True, follow_redirects=False)
    except httpx2.InvalidURL:
        return httpx2.AsyncClient(trust_env=False, follow_redirects=False)


@asynccontextmanager
async def ai_client(config: AIConfig) -> AsyncIterator[openai.AsyncOpenAI]:
    if not config.configured or config.api_key is None:
        raise AIError('ai_not_configured', '请先配置 AI 模型与 Key', 409)
    transport = None
    try:
        transport = _transport()
        client = openai.AsyncOpenAI(
            api_key=config.api_key.get_secret_value(), base_url=config.url,
            http_client=transport, max_retries=0, timeout=float(config.timeout_seconds),
        )
        with anyio.fail_after(config.timeout_seconds):
            yield client
    except (openai.APITimeoutError, TimeoutError) as exc:
        raise AIError('upstream_timeout', 'AI 上游响应超时', 504) from exc
    except openai.APIStatusError as exc:
        if exc.status_code in (401, 403):
            raise AIError('upstream_auth', 'AI 上游拒绝了凭据或模型权限') from exc
        if exc.status_code == 429:
            raise AIError('upstream_rate_limit', 'AI 上游限流或额度不足', 429) from exc
        raise AIError('upstream_status', f'AI 上游请求失败（HTTP {exc.status_code}）') from exc
    except (openai.OpenAIError, httpx2.HTTPError, httpx2.InvalidURL, ValueError) as exc:
        raise AIError('upstream_error', '无法读取 AI 上游响应，请检查地址、代理和协议') from exc
    finally:
        if transport is not None:
            with anyio.CancelScope(shield=True):
                await transport.aclose()


async def test_connection(config: AIConfig) -> None:
    async with ai_client(config) as client:
        if config.protocol == 'openai':
            result = await client.chat.completions.create(
                model=config.model, messages=[{'role': 'user', 'content': 'Reply OK.'}], max_completion_tokens=16,
            )
            if not result.choices:
                raise _bad_stream()
        else:
            result = await client.responses.create(
                model=config.model, input='Reply OK.', max_output_tokens=16, store=False,
            )
            if result.status != 'completed':
                raise _bad_stream()


async def stream_text(config: AIConfig, prompt: str, system: str) -> AsyncIterator[str]:
    filter_ = KeyFilter(config.api_key.get_secret_value() if config.api_key else '')
    count = 0
    has_text = False
    async with ai_client(config) as client:
        chunks = (_chat_stream if config.protocol == 'openai' else _responses_stream)(
            client, config.model, prompt, system,
        )
        async for chunk in chunks:
            count += len(chunk)
            if count > MAX_OUTPUT_CHARS:
                raise AIError('output_limit', 'AI 输出超过长度上限')
            if text := filter_.feed(chunk):
                has_text |= bool(text.strip())
                yield text
        if text := filter_.feed('', final=True):
            has_text |= bool(text.strip())
            yield text
        if not has_text:
            raise _bad_stream()


async def _chat_stream(client, model, prompt, system) -> AsyncIterator[str]:
    stream = await client.chat.completions.create(
        model=model, messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}],
        max_completion_tokens=2048, stream=True,
    )
    finish = None
    async for chunk in stream:
        for choice in chunk.choices or []:
            if choice.index != 0:
                continue
            delta = choice.delta
            if choice.finish_reason:
                finish = choice.finish_reason
            if delta.tool_calls:
                raise _bad_stream()
            if delta.content:
                yield delta.content
    if finish != 'stop':
        raise _bad_stream()


async def _responses_stream(client, model, prompt, system) -> AsyncIterator[str]:
    stream = await client.responses.create(
        model=model, instructions=system, input=prompt, max_output_tokens=2048, stream=True, store=False,
    )
    completed = None
    deltas = False
    async for event in stream:
        if event.type == 'response.output_text.delta':
            deltas = True
            yield event.delta
        elif event.type == 'response.completed':
            completed = event.response
        elif event.type in ('error', 'response.failed', 'response.incomplete'):
            raise _bad_stream()
    if completed is None or completed.status != 'completed':
        raise _bad_stream()
    if any(item.type == 'function_call' for item in completed.output):
        raise _bad_stream()
    if not deltas and completed.output_text:
        yield completed.output_text
