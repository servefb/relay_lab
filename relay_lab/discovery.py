"""Read a site's model IDs without ever starting inference."""

from __future__ import annotations

import json
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import ModelsRequest, ModelsResponse

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_MODELS = 1000


class DiscoveryError(Exception):
    """An intentionally sanitized error safe to return to the browser."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def models_endpoint(url: str) -> str:
    """Keep custom proxy prefixes while replacing known inference endpoint suffixes."""
    parts = urlsplit(url)
    path = parts.path.rstrip('/')
    if path.endswith('/models'):
        return urlunsplit((parts.scheme, parts.netloc, path, '', ''))
    for suffix in ('/chat/completions', '/responses', '/completions'):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if not path:
        path = '/v1'
    return urlunsplit((parts.scheme, parts.netloc, path + '/models', '', ''))


def fetch_models(connection: ModelsRequest) -> ModelsResponse:
    """GET one list only; never follow redirects, infer model IDs, or invoke generation."""
    endpoint = models_endpoint(connection.url)
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity'}
    if connection.api_key:
        headers['Authorization'] = f'Bearer {connection.api_key.get_secret_value().strip()}'
    messages = {
        401: '获取失败：API Key 无效或已过期，请检查密钥。',
        403: '获取失败：该 Key 没有读取模型列表的权限。',
        404: '该站点未提供此模型列表接口，请检查 API 地址或联系站点管理员开放 /v1/models。',
        429: '模型列表接口被限流，请稍后再试。',
    }
    try:
        started = time.monotonic()
        with httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False, trust_env=False) as client:
            with client.stream('GET', endpoint, headers=headers) as response:
                if response.status_code != 200:
                    if 300 <= response.status_code < 400:
                        raise DiscoveryError('模型列表接口返回重定向；为避免转发密钥，请直接填写最终 API 地址。')
                    raise DiscoveryError(messages.get(response.status_code, '获取模型列表失败：站点返回异常状态。'))
                content = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > 12:
                        raise DiscoveryError('获取模型列表超时，请检查网络或站点状态。', 504)
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise DiscoveryError('模型列表响应过大，已停止读取；请联系站点管理员。')
    except (httpx.InvalidURL, UnicodeError):
        raise DiscoveryError('API 地址或密钥格式不正确，请检查后重试。', 400) from None
    except httpx.TimeoutException:
        raise DiscoveryError('获取模型列表超时，请检查网络或站点状态。', 504) from None
    except httpx.RequestError:
        raise DiscoveryError('无法连接模型列表接口，请检查地址、网络和 HTTPS 证书。') from None
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeError):
        raise DiscoveryError('站点没有返回有效 JSON 模型列表，可能填写了网站首页而不是 API 地址。') from None
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        raise DiscoveryError('模型列表格式不兼容，预期为 data 数组中的 id 字段。')
    identifiers = {
        item['id']
        for item in payload['data']
        if isinstance(item, dict)
        and isinstance(item.get('id'), str)
        and item['id'].strip()
        and len(item['id']) <= 200
        and not any(ord(character) < 32 for character in item['id'])
    }
    return ModelsResponse(
        models=sorted(identifiers)[:MAX_MODELS],
        endpoint=endpoint,
        truncated=len(identifiers) > MAX_MODELS or payload.get('has_more') is True,
    )
