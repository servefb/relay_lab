"""Explain a stage where every request failed.

The vendored engine only persists success flags: the HTTP status and body of a
rejected request never reach SQLite or the report, so a 0% stage used to say
"check the native log" with nothing to check. When a stage fails outright we
send one minimal request with the same endpoint, model and key, and turn the
answer into one readable line.

This is deliberately one extra request per run, only on total failure, and it
never runs for a stopped job.
"""

from __future__ import annotations

import httpx

from .engine import endpoint_of

STATUS_HINTS = {
    400: '请求被拒，多为参数不被该站点接受',
    401: '认证失败，Key 无效或没有随请求发出',
    402: '余额或额度不足',
    403: '无权访问，Key 可能没有这个模型的权限',
    404: '路径或模型不存在，检查地址是否需要带 /v1、或模型名是否拼错',
    408: '上游超时',
    413: '请求体过大',
    422: '参数格式不被接受',
    429: '被限流，账户当前额度或并发已用尽',
}
"""Statuses that say something actionable about configuration."""

SNIPPET = 180
"""How much of the upstream body to quote; enough to identify the error code."""


def _url(settings: object) -> str:
    """Rebuild the same endpoint the load generator used."""
    base = endpoint_of(settings.url)  # type: ignore[attr-defined]
    if settings.protocol == 'openai_responses':  # type: ignore[attr-defined]
        return base if base.rstrip('/').endswith('/responses') else base.rstrip('/') + '/responses'
    return base if base.rstrip('/').endswith('/chat/completions') else base.rstrip('/') + '/chat/completions'


def _body(settings: object) -> dict:
    if settings.protocol == 'openai_responses':  # type: ignore[attr-defined]
        return {
            'model': settings.model,  # type: ignore[attr-defined]
            'input': 'ping',
            'max_output_tokens': 16,
            'stream': False,
        }
    return {
        'model': settings.model,  # type: ignore[attr-defined]
        'messages': [{'role': 'user', 'content': 'ping'}],
        'max_tokens': 1,
        # Same constraint as contract/quality: thinking routes require temperature=1.
        'temperature': 1,
        'stream': False,
    }


def explain(settings: object, api_key: str | None) -> str:
    """Return one line describing why a single request succeeds or fails."""
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    timeout = min(20, max(3, getattr(settings, 'request_timeout', 15)))
    try:
        response = httpx.post(
            _url(settings),
            headers=headers,
            json=_body(settings),
            timeout=timeout,
            follow_redirects=False,
        )
    except httpx.TimeoutException:
        return f'单请求诊断：{timeout}s 内没有响应（超时）'
    except httpx.RequestError as error:
        return f'单请求诊断：连接失败（{type(error).__name__}）'
    except httpx.InvalidURL:
        # Only reachable when the machine's own proxy settings are unparsable; the
        # diagnosis must say that rather than disappear into a generic failure.
        return '单请求诊断：本机代理配置无法解析，未能发出诊断请求'
    if response.status_code == 200:
        return '单请求诊断：单个请求可以正常返回，失败更可能来自并发、速率或站点侧限流'
    hint = STATUS_HINTS.get(response.status_code, '站点返回了非 200 响应')
    snippet = response.text.replace('\n', ' ')[:SNIPPET].strip()
    detail = f'：{snippet}' if snippet else ''
    return f'单请求诊断：HTTP {response.status_code} · {hint}{detail}'
