"""Built-in smoke quality suite: fixed items, deterministic graders.

This is not a benchmark. It answers one question: does this model, on this
relay, still follow instructions and reason well enough to be usable? Graders
are local and rule-based, so a pass never depends on another model's opinion.

Items are spread over three difficulty levels on purpose: an easy-only suite
saturates at 100% for every competent model and therefore says nothing about
quality differences. Levels are reported separately, so "8/8 简单 + 0/2 困难"
stays visible instead of being averaged into one number.
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
from .quality_media import CHART_B64, FRAME_B64S, SHAPES_B64, SIGN_B64, data_url

CATEGORIES = list(QUALITY_CATEGORIES)
LEVELS = ['简单', '中等', '困难']


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


GRADERS: dict[str, Callable[[str, str], bool]] = {
    'number': _number,
    'contains': _contains,
    'any': _any,
    'exact': _exact,
    'starts': _starts,
    'json': _json,
    'python': _python,
}


SUITE: list[Item] = [
    # ---------- 算术：多步计算、单位陷阱、小数 ----------
    Item('算术', '37 乘以 24 等于多少？只输出数字。', '888', 'number', '简单'),
    Item('算术', '240 的 15% 是多少？只输出数字。', '36', 'number', '简单'),
    Item('算术', '一个班 48 人，其中 3/8 是女生，女生有多少人？只输出数字。', '18', 'number', '简单'),
    Item('算术', '一件商品先涨价 20%，再降价 20%。最终价格是原价的百分之多少？只输出数字。', '96', 'number', '中等'),
    Item('算术', '从 1 加到 100 等于多少？只输出数字。', '5050', 'number', '中等'),
    Item('算术', '3 个人 6 天能完成一项工作，同样效率下 5 个人需要几天？保留一位小数。', '3.6', 'number', '中等'),
    Item('算术', '甲管 6 小时注满水池，乙管 4 小时注满，两管同时开需要几小时？保留一位小数。', '2.4', 'number', '困难'),
    Item('算术', '鸡兔同笼，共 35 个头、94 只脚。兔子有多少只？只输出数字。', '12', 'number', '困难'),
    # ---------- 指令：格式、长度、变换 ----------
    Item('指令', '只回复两个字：可以', '可以', 'exact', '简单'),
    Item('指令', '把 Hello World 转成大写，只输出结果。', 'HELLO WORLD', 'exact', '简单'),
    Item('指令', '用一句话回答，并且必须以“因为”开头：天空为什么是蓝色的？', '因为', 'starts', '简单'),
    Item('指令', '输出三个交通工具名称，用中文逗号分隔，不要任何解释。', '，', 'contains', '简单'),
    Item('指令', '把单词 relay 倒序输出，只输出结果。', 'yaler', 'exact', '中等'),
    Item('指令', '用中文数字按顺序输出 1 到 5，用顿号分隔，不要解释。', '一、二、三、四、五', 'exact', '中等'),
    Item('指令', '把这句话改写成被动句，只输出改写结果：团队完成了项目。', '被|完成', 'contains', '中等'),
    Item('指令', '把十进制 42 转成二进制，只输出数字。', '101010', 'exact', '困难'),
    # ---------- JSON：结构、嵌套、类型 ----------
    Item('JSON', '只输出一个 JSON 对象，不要代码块，字段 ok 的值为 true。', '{"ok": true}', 'json', '简单'),
    Item('JSON', '只输出一个 JSON 对象，不要代码块，字段 name 的值为 relay。', '{"name": "relay"}', 'json', '简单'),
    Item('JSON', '只输出一个 JSON 数组，包含 1、2、3 三个数字。', '[1, 2, 3]', 'json', '简单'),
    Item(
        'JSON',
        '只输出一个 JSON 对象，字段 code 等于 200，字段 msg 等于 ready。',
        '{"code": 200, "msg": "ready"}',
        'json',
        '简单',
    ),
    Item(
        'JSON',
        '只输出一个 JSON 对象，包含嵌套对象 user，user 里 id 为 7、vip 为 false。',
        '{"user": {"id": 7, "vip": false}}',
        'json',
        '中等',
    ),
    Item(
        'JSON',
        '只输出一个 JSON 数组，两个元素都是对象，第一个 id 为 1、ok 为 true，第二个 id 为 2、ok 为 false。',
        '[{"id": 1, "ok": true}, {"id": 2, "ok": false}]',
        'json',
        '中等',
    ),
    Item(
        'JSON',
        '只输出一个 JSON 对象：字段 tags 是 ["a","b","c"]，字段 count 是 3。',
        '{"tags": ["a", "b", "c"], "count": 3}',
        'json',
        '中等',
    ),
    Item(
        'JSON',
        '只输出一个 JSON 对象，字段 text 的值是字符 a、一个双引号、字符 b 组成的字符串。',
        '{"text": "a\\"b"}',
        'json',
        '困难',
    ),
    # ---------- 中文理解：指代、否定、改写、语义 ----------
    Item('中文理解', '找出这句话里你将要去的城市，只输出城市名：我下周从杭州飞往成都。', '成都', 'exact', '简单'),
    Item('中文理解', '找出这句话的主语，只输出主语：小猫在窗台上睡觉。', '小猫', 'exact', '简单'),
    Item('中文理解', '把这句话改成否定句，只输出改写后的句子：他今天会来。', '不', 'contains', '简单'),
    Item('中文理解', '把“今天下雨了”翻译成英文，只输出英文。', 'rain', 'any', '简单'),
    Item('中文理解', '“小明把书给了小红，因为她明天要用。”这句话里“她”指的是谁？只输出名字。', '小红', 'exact', '中等'),
    Item('中文理解', '“他不是不想去。”他的意思是想去还是不想去？只回答“去”或“不去”。', '去', 'exact', '中等'),
    Item(
        '中文理解',
        '把“虽然下雨了，但是比赛照常进行”改写成一个不用“虽然…但是”的句子，保留原意，并必须包含“仍然”。',
        '仍然',
        'contains',
        '中等',
    ),
    Item('中文理解', '“画蛇添足”比喻做了什么？用一句话回答，且必须包含“多余”。', '多余', 'contains', '困难'),
    # ---------- 代码：可解析、结构检查 ----------
    Item('代码', '写一个 Python 函数 add(a, b)，只输出代码。', 'def add|return', 'contains', '简单'),
    Item('代码', '用 Python 打印 1 到 5，只输出代码。', 'print|range', 'contains', '简单'),
    Item('代码', '用一句话说明这段代码做什么：x = [i * i for i in range(3)]', '平方|0, 1, 4', 'any', '简单'),
    Item('代码', '写一条 SQL，从 users 表里选出 name 列，只输出 SQL。', 'select|from|users', 'contains', '简单'),
    Item(
        '代码', '写一个 Python 函数 is_even(n)，返回 n 是否为偶数，只输出代码。', 'is_even|return|%', 'python', '中等'
    ),
    Item(
        '代码',
        '写一条 SQL：从 users 表选出 age 大于 18 的 name，按 name 升序。只输出 SQL。',
        'select|where|order by',
        'contains',
        '中等',
    ),
    Item(
        '代码',
        '这段 Python 想求平均值但结果是错的：total = 0，循环里 total += x，最后 return total。'
        '用一句话指出问题，必须包含“平均值”或“长度”。',
        '平均值|长度|len',
        'any',
        '困难',
    ),
    Item(
        '代码', '用 Python 写递归函数 fact(n) 计算阶乘，n 为 0 时返回 1，只输出代码。', 'fact|return', 'python', '困难'
    ),
    # ---------- 逻辑推理：三段论、数列、真假话、约束 ----------
    Item('逻辑推理', '所有人都会死，苏格拉底是人。苏格拉底会死吗？只回答“会”或“不会”。', '会', 'exact', '简单'),
    Item('逻辑推理', '数列 2、4、8、16、? 的下一项是多少？只输出数字。', '32', 'number', '简单'),
    Item('逻辑推理', '9 个球里有一个偏重，用天平最少称几次一定能找出它？只输出数字。', '2', 'number', '简单'),
    Item(
        '逻辑推理',
        '甲说“乙在说谎”，乙说“丙在说谎”，丙说“甲和乙都在说谎”。已知只有一个人说真话，是谁？只回答名字。',
        '乙',
        'exact',
        '中等',
    ),
    Item(
        '逻辑推理',
        '“并非所有人都及格”等价于下面哪句？只输出改写后的句子：所有人都没及格 / 有人没及格。',
        '有人没及格',
        'contains',
        '中等',
    ),
    Item('逻辑推理', '同时抛两枚均匀硬币，至少出现一个正面的概率是多少？只输出分数。', '3/4', 'exact', '中等'),
    Item(
        '逻辑推理', '甲乙丙丁四人赛跑。乙在甲前面，甲在丙前面，丁在丙后面。谁第一？只回答名字。', '乙', 'exact', '困难'
    ),
    Item(
        '逻辑推理',
        '三只箱子分别标“苹果”“橘子”“混合”，标签全错。只从一个箱子取一个水果，最少取几次能确定全部内容？只输出数字。',
        '1',
        'number',
        '困难',
    ),
    # ---------- 多模态：图片与视频帧 ----------
    Item('多模态', '图中凭单上印的通行码数字是多少？只输出数字。', '5827', 'number', '简单', (data_url(SIGN_B64),)),
    Item('多模态', '数一数图里画了几个圆形，只输出数字。', '3', 'number', '简单', (data_url(SHAPES_B64),)),
    Item('多模态', '这是柱状图。最高的那根柱子下方标的数字是几？只输出数字。', '3', 'number', '中等', (data_url(CHART_B64),)),
    Item(
        '多模态',
        '下面四张图是同一段视频按时间顺序截取的帧。图中的实心圆在向左还是向右移动？只回答“左”或“右”。',
        '右',
        'contains',
        '中等',
        tuple(data_url(frame) for frame in FRAME_B64S),
    ),
]


def select(settings: QualitySettings) -> list[Item]:
    """Filter by category and cap per category; the order stays stable."""
    wanted = [item for item in SUITE if not settings.categories or item.category in settings.categories]
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
            'temperature': 0,
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
            'temperature': 0,
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
