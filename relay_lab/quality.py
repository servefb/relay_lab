"""Built-in quality smoke packs: fixed items, deterministic graders.

This is not a full benchmark. It answers one question: does this model, on this
relay, still follow instructions and reason well enough to be usable? Graders
are local and rule-based, so a pass never depends on another model's opinion.

The default `standard` pack is an MMLU multiple-choice subset (letter-only).
The `smoke` pack keeps the original Chinese hand set. Levels are reported
separately so easy/hard mix stays visible.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import netutil
from .config import QUALITY_CATEGORIES, CategoryScore, QualityItem, QualitySettings, QualitySummary
from .quality_packs import PACK_META, SMOKE_ROWS, STANDARD_ROWS

CATEGORIES = list(QUALITY_CATEGORIES)
LEVELS = ['简单', '中等', '困难']
PACK_LABELS = PACK_META


@dataclass
class Answer:
    """One model reply plus the timings we measured ourselves."""

    text: str = ''
    ttft_ms: float | None = None
    latency_ms: float | None = None
    tokens: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class Item:
    category: str
    prompt: str
    expected: str
    grader: str
    level: str = '简单'
    # Data URLs attached to the message; empty for plain-text items. Video is
    # represented as sequential frames because Chat Completions has no standard
    # video field — frame sequences are what vision-capable endpoints accept.
    images: tuple[str, ...] = ()
    source: str = ''


def _items_from_rows(rows: list) -> list[Item]:
    return [
        Item(
            category=category,
            prompt=prompt,
            expected=expected,
            grader=grader,
            level=level,
            images=images,
            source=source,
        )
        for category, prompt, expected, grader, level, images, source in rows
    ]


PACKS: dict[str, list[Item]] = {
    'standard': _items_from_rows(STANDARD_ROWS),
    'smoke': _items_from_rows(SMOKE_ROWS),
}
SUITE: list[Item] = PACKS['standard']


def _number(text: str, expected: str) -> bool:
    """Compare the last number in the answer, so working shown before it still passes."""
    numbers = re.findall(r'-?\d+(?:\.\d+)?', text.replace(',', '').replace('，', ''))
    if not numbers:
        return False
    try:
        return abs(float(numbers[-1]) - float(expected)) < 1e-9
    except ValueError:
        return False


def _contains(text: str, expected: str) -> bool:
    low = text.lower()
    return all(word.lower() in low for word in expected.split('|'))


def _any(text: str, expected: str) -> bool:
    low = text.lower()
    return any(word.lower() in low for word in expected.split('|'))


def _exact(text: str, expected: str) -> bool:
    return text.strip().strip('。.!！"“”`').lower() == expected.lower()


def _starts(text: str, expected: str) -> bool:
    return text.strip().startswith(expected)


def _json(text: str, expected: str) -> bool:
    body = text.strip()
    fenced = re.search(r'```(?:json)?\s*(.+?)```', body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    start = min([index for index in (body.find('{'), body.find('[')) if index >= 0], default=-1)
    if start < 0:
        return False
    try:
        payload, _ = json.JSONDecoder().raw_decode(body[start:])
    except ValueError:
        return False
    wanted = json.loads(expected)
    if isinstance(payload, dict) and isinstance(wanted, dict):
        if not all(payload.get(key) == value for key, value in wanted.items()):
            return False
        # Python treats True == 1, so a model that answers {"ok": 1} would otherwise
        # pass a question that explicitly asked for a JSON boolean.
        return all(isinstance(payload.get(key), bool) for key, value in wanted.items() if isinstance(value, bool))
    return payload == wanted


def _python(text: str, expected: str) -> bool:
    """Require real, parsable Python with the asked-for function, not just the words."""
    parts = expected.split('|')
    name, required = parts[0], parts[1:]
    source = text.strip()
    fenced = re.search(r'```(?:python)?\s*(.+?)```', source, re.S)
    if fenced:
        source = fenced.group(1)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
    if name not in defined:
        return False
    low = source.lower()
    return all(word.lower() in low for word in required)


def _choice(text: str, expected: str) -> bool:
    """Grade MMLU-style A/B/C/D answers; tolerate Answer: B / (B) / B. wrappers."""
    wanted = expected.strip().upper()
    if wanted not in {'A', 'B', 'C', 'D'}:
        return False
    body = text.strip().upper()
    matches = re.findall(r'(?<![A-Z])([ABCD])(?![A-Z])', body)
    if matches:
        return matches[-1] == wanted
    return body.strip('.。)）') == wanted


GRADERS: dict[str, Callable[[str, str], bool]] = {
    'number': _number,
    'contains': _contains,
    'any': _any,
    'exact': _exact,
    'starts': _starts,
    'json': _json,
    'python': _python,
    'choice': _choice,
}


def select(settings: QualitySettings) -> list[Item]:
    """Filter by pack + category and cap per category; the order stays stable."""
    suite = PACKS.get(settings.pack) or PACKS['standard']
    wanted = [item for item in suite if not settings.categories or item.category in settings.categories]
    counts: dict[str, int] = {}
    chosen: list[Item] = []
    for item in wanted:
        used = counts.get(item.category, 0)
        if used >= settings.per_category:
            continue
        counts[item.category] = used + 1
        chosen.append(item)
    return chosen


def grade(item: Item, answer: str) -> bool:
    """Never raise: a grader bug must not turn into a fake pass."""
    try:
        return GRADERS[item.grader](answer, item.expected)
    except (ValueError, TypeError, IndexError, KeyError):
        return False


def _endpoint(settings: QualitySettings) -> str:
    parts = urlsplit(settings.url)
    path = parts.path.rstrip('/')
    for suffix in ('/chat/completions', '/responses'):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if not path:
        path = '/v1'
    tail = '/responses' if settings.protocol == 'openai_responses' else '/chat/completions'
    return urlunsplit((parts.scheme, parts.netloc, path + tail, '', ''))


async def _call(
    client: httpx.AsyncClient,
    settings: QualitySettings,
    api_key: str | None,
    item: Item,
    timeout: float,
) -> Answer:
    """One streaming call; the answer text and timings, never a guess."""
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    responses = settings.protocol == 'openai_responses'
    if responses:
        # Responses API: a string input for text items, message parts for images
        # (input_text / input_image instead of the chat-completions part names).
        value: str | list[dict[str, str]] = item.prompt
        if item.images:
            value = [{'role': 'user', 'content': [{'type': 'input_text', 'text': item.prompt}] + [
                {'type': 'input_image', 'image_url': image} for image in item.images
            ]}]
        body = {
            'model': settings.model,
            'input': value,
            'max_output_tokens': 512,
            # Kimi / GLM thinking routes (and some OpenAI reasoning models) reject
            # any temperature other than 1; graders are local so sampling still ok.
            'temperature': 1,
            'stream': True,
        }
    else:
        # Chat Completions: plain string content for text items keeps every
        # endpoint compatible; multimodal switches to the content-parts array.
        content: str | list[dict[str, str]] = item.prompt
        if item.images:
            content = [{'type': 'text', 'text': item.prompt}] + [
                {'type': 'image_url', 'image_url': {'url': image}} for image in item.images
            ]
        body = {
            'model': settings.model,
            'messages': [{'role': 'user', 'content': content}],
            'max_tokens': 512,
            'temperature': 1,
            'stream': True,
            'stream_options': {'include_usage': True},
        }
    started = time.monotonic()
    first: float | None = None
    pieces: list[str] = []
    tokens: int | None = None
    try:
        async with client.stream('POST', _endpoint(settings), headers=headers, json=body, timeout=timeout) as response:
            if response.status_code != 200:
                try:
                    body_text = (await response.aread()).decode('utf-8', 'replace')
                except Exception:  # noqa: BLE001 - the status alone is still actionable
                    body_text = ''
                snippet = ' '.join(body_text.split())[:180]
                return Answer(error=f'HTTP {response.status_code}' + (f'：{snippet}' if snippet else ''))
            async for line in response.aiter_lines():
                if not line.startswith('data:'):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == '[DONE]':
                    continue
                try:
                    payload = json.loads(chunk)
                except ValueError:
                    continue
                if responses:
                    # Responses API events: typed JSON objects, usage inside the
                    # final response.completed envelope.
                    event = payload.get('type')
                    if event == 'response.output_text.delta':
                        text = payload.get('delta') or ''
                        if text:
                            if first is None:
                                first = time.monotonic()
                            pieces.append(text)
                    elif event == 'response.completed':
                        usage = (payload.get('response') or {}).get('usage') or {}
                        if isinstance(usage.get('output_tokens'), int):
                            tokens = usage['output_tokens']
                    elif event in ('response.failed', 'error'):
                        detail = payload.get('response', payload).get('error') or payload.get('error') or payload
                        snippet = ' '.join(json.dumps(detail, ensure_ascii=False).split())[:180]
                        return Answer(text=''.join(pieces), error=f'HTTP 200 但事件异常：{snippet}')
                else:
                    usage = payload.get('usage') or {}
                    if isinstance(usage.get('completion_tokens'), int):
                        tokens = usage['completion_tokens']
                    choices = payload.get('choices') or []
                    if choices:
                        delta = choices[0].get('delta') or {}
                        text = delta.get('content') or ''
                        if text:
                            if first is None:
                                first = time.monotonic()
                            pieces.append(text)
    except httpx.TimeoutException:
        return Answer(text=''.join(pieces), error='超时')
    except httpx.RequestError as error:
        return Answer(text=''.join(pieces), error=f'连接失败（{type(error).__name__}）')
    finished = time.monotonic()
    return Answer(
        text=''.join(pieces),
        ttft_ms=(first - started) * 1000 if first else None,
        latency_ms=(finished - started) * 1000,
        tokens=tokens,
    )


def summarize_items(results: list[QualityItem]) -> QualitySummary:
    """Summary over whatever items are done so far; no token accounting yet.

    Called after every completed item so the UI can show one result at a
    time instead of a single wall of numbers at the very end.
    """
    if not results:
        return QualitySummary(total=0, passed=0, accuracy=0.0)
    passed = sum(1 for item in results if item.passed)
    ttfts = sorted(item.ttft_ms for item in results if item.ttft_ms is not None)
    latencies = [item.latency_ms for item in results if item.latency_ms is not None]
    by_category: dict[str, CategoryScore] = {}
    by_level: dict[str, CategoryScore] = {}
    for item in results:
        category_score = by_category.setdefault(item.category, CategoryScore())
        category_score.total += 1
        category_score.passed += int(item.passed)
        level_score = by_level.setdefault(item.level or '未知', CategoryScore())
        level_score.total += 1
        level_score.passed += int(item.passed)
    return QualitySummary(
        total=len(results),
        passed=passed,
        accuracy=round(100 * passed / len(results), 1),
        by_category=by_category,
        by_level=by_level,
        ttft_p50_ms=round(ttfts[len(ttfts) // 2], 1) if ttfts else None,
        latency_avg_ms=round(sum(latencies) / len(latencies), 1) if latencies else None,
    )


async def _run(
    settings: QualitySettings,
    api_key: str | None,
    should_stop: Callable[[], bool],
    on_item: Callable[[QualityItem], None] | None = None,
) -> tuple[list[QualityItem], QualitySummary]:
    items = select(settings)
    results: list[QualityItem] = []
    semaphore = asyncio.Semaphore(settings.max_parallel)

    async def one(index: int, item: Item) -> tuple[QualityItem, int | None]:
        async with semaphore:
            if should_stop():
                empty = QualityItem(
                    index=index,
                    category=item.category,
                    level=item.level,
                    prompt=item.prompt,
                    expected=item.expected,
                    source=item.source,
                    media_count=len(item.images),
                    error='已取消',
                )
                return empty, None
            reply = await _call(client, settings, api_key, item, settings.request_timeout)
            passed = bool(reply.text) and reply.error is None and grade(item, reply.text)
            record = QualityItem(
                index=index,
                category=item.category,
                level=item.level,
                prompt=item.prompt,
                expected=item.expected,
                source=item.source,
                answer=reply.text[:2000],
                passed=passed,
                ttft_ms=reply.ttft_ms,
                latency_ms=reply.latency_ms,
                error=reply.error,
                media_count=len(item.images),
            )
            # Push as soon as this item is graded, before the batch finishes;
            # the caller persists and the UI picks it up on its next poll.
            if on_item is not None:
                on_item(record)
            return record, reply.tokens

    limits = httpx.Limits(max_connections=settings.max_parallel + 2, max_keepalive_connections=settings.max_parallel)
    # trust_env matches the load generator: if this machine needs a proxy to reach the
    # site, capacity and quality must both use it, or the two disagree for no reason.
    # It is gated because an unparsable NO_PROXY entry makes client construction itself
    # raise, which would fail every item before a request is sent (see netutil).
    async with httpx.AsyncClient(
        limits=limits, follow_redirects=False, trust_env=netutil.trust_env_usable()
    ) as client:
        pairs = await asyncio.gather(*(one(index, item) for index, item in enumerate(items, start=1)))
    results = [pair[0] for pair in pairs]
    tokens = sum(pair[1] for pair in pairs if pair[1])

    summary = summarize_items(results)
    latencies = [item.latency_ms for item in results if item.latency_ms is not None]
    busy = sum(latencies) / 1000 if latencies else 0
    if tokens and busy:
        summary.output_tps = round(tokens / busy, 1)
    return results, summary


def run_suite(
    settings: QualitySettings,
    api_key: str | None,
    should_stop: Callable[[], bool] = lambda: False,
    on_item: Callable[[QualityItem], None] | None = None,
) -> tuple[list[QualityItem], QualitySummary]:
    """Blocking entry point used from the lab's worker thread."""
    return asyncio.run(_run(settings, api_key, should_stop, on_item))
