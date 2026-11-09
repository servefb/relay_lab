"""Speed, parallel, quality and schedule behaviour, without network access."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from relay_lab import contract, quality
from relay_lab.api import create_app
from relay_lab.config import (
    ContractRequest,
    ParallelRequest,
    ParallelSettings,
    QualityRequest,
    QualitySettings,
    ScheduleRequest,
    SpeedRequest,
    SpeedSettings,
)
from relay_lab.scheduler import Scheduler
from relay_lab.search import CapacityLab
from relay_lab.vendor.perf.utils.perf_models import BenchmarkSummary, PercentileResult, PercentileRow


def _half(value):
    """Keep the None case intact for the missing-metric tests."""
    return None if value is None else value / 2


def native(*, succeeded=5, total=5, seconds=5, ttft=100.0, tpot=20.0) -> tuple:
    return (
        BenchmarkSummary(
            time_taken=seconds,
            total_requests=total,
            succeed_requests=succeeded,
            avg_input_tokens=24,
            avg_output_tokens=16,
            avg_latency=0.5,
        ),
        PercentileResult(
            rows=[
                PercentileRow(percentile='50%', ttft=_half(ttft), tpot=_half(tpot), latency=0.4),
                PercentileRow(percentile='99%', ttft=ttft, tpot=tpot, latency=0.9),
            ]
        ),
        None,
        None,
    )


def wait_done(lab: CapacityLab, run_id: str):
    assert lab.jobs[run_id].done.wait(10)
    return next(item for item in lab.history() if item.run_id == run_id)


# ---------- speed ----------


def test_speed_run_records_latency_percentiles(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)
    calls = []

    def fake(arguments, task_id, timeout):
        calls.append(arguments)
        return native(total=arguments.number[0], succeeded=arguments.number[0])

    request = SpeedRequest(url='http://127.0.0.1:19999', model='m', authorized=True, requests=5, max_parallel=1)
    with patch('relay_lab.search._execute', fake):
        run = lab.start_speed(request)
        report = wait_done(lab, run.run_id)
    assert report.kind == 'speed'
    assert len(report.stages) == 1
    stage = report.stages[0]
    assert stage.phase == 'speed'
    assert stage.requests == 5
    assert stage.ttft_p50_ms == 50
    assert stage.ttft_p99_ms == 100
    assert stage.latency_avg_ms == 500
    assert stage.status == 'passed'
    # A fixed-count run must not carry a duration deadline.
    assert calls[0].duration is None
    assert calls[0].rate[0] == -1


# ---------- parallel ----------


def test_parallel_run_visits_every_level(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)
    seen = []

    def fake(arguments, task_id, timeout):
        seen.append((arguments.parallel[0], arguments.number[0], arguments.duration))
        return native(total=arguments.number[0], succeeded=arguments.number[0])

    request = ParallelRequest(url='http://127.0.0.1:19999', model='m', authorized=True, levels=[4, 2], stage_seconds=5)
    with patch('relay_lab.search._execute', fake):
        run = lab.start_parallel(request)
        report = wait_done(lab, run.run_id)
    assert report.kind == 'parallel'
    assert [stage.pressure for stage in report.stages] == [2, 4]
    assert [item[0] for item in seen] == [2, 4]
    assert all(item[2] == 5 for item in seen)
    assert all(stage.status == 'passed' for stage in report.stages)


def test_parallel_levels_are_sorted_and_bounded() -> None:
    settings = ParallelSettings(url='http://127.0.0.1:1', model='m', levels=[8, 2, 8, 1])
    assert settings.levels == [1, 2, 8]
    with pytest.raises(ValueError):
        ParallelSettings(url='http://127.0.0.1:1', model='m', levels=[0])
    with pytest.raises(ValueError):
        ParallelSettings(url='http://127.0.0.1:1', model='m', levels=[256])


# ---------- quality ----------


@pytest.mark.parametrize(
    ('prompt_part', 'answer', 'expected'),
    [
        ('37 乘以 24', '计算过程：37 × 24 = 888', True),
        ('37 乘以 24', '答案是 887', False),
        ('女生有多少人', '18 人', True),
        ('只回复两个字', '可以', True),
        ('只回复两个字', '当然可以', False),
        ('转成大写', '"HELLO WORLD"', True),
        ('字段 ok 的值为 true', '```json\n{"ok": true}\n```', True),
        ('字段 ok 的值为 true', '{"ok": false}', False),
        ('包含 1、2、3 三个数字', '[1, 2, 3]', True),
        ('包含 1、2、3 三个数字', '[1,2,3,4]', False),
        ('飞往成都', '成都', True),
        ('函数 add(a, b)', 'def add(a, b):\n    return a + b', True),
    ],
)
def test_graders_accept_explanations_but_not_wrong_answers(prompt_part: str, answer: str, expected: bool) -> None:
    item = next(entry for entry in quality.SUITE if prompt_part in entry.prompt)
    assert quality.grade(item, answer) is expected


def test_suite_selection_respects_category_and_count() -> None:
    settings = QualitySettings(url='http://127.0.0.1:1', model='m', categories=['算术'], per_category=2)
    chosen = quality.select(settings)
    assert len(chosen) == 2
    assert {item.category for item in chosen} == {'算术'}
    assert len(quality.select(QualitySettings(url='http://127.0.0.1:1', model='m', per_category=20))) == len(
        quality.SUITE
    )


def test_quality_run_fills_items_and_summary(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)

    async def fake_call(client, settings, api_key, item, timeout):
        return quality.Answer(text='可以' if '只回复两个字' in item.prompt else '', ttft_ms=120.0, latency_ms=300.0, tokens=4)

    request = QualityRequest(
        url='http://127.0.0.1:19999', model='m', authorized=True, categories=['指令'], per_category=1
    )
    with patch.object(quality, '_call', fake_call):
        run = lab.start_quality(request)
        report = wait_done(lab, run.run_id)
    assert report.kind == 'quality'
    assert report.quality is not None
    assert report.quality.total == 1
    assert report.quality.passed == 1
    assert report.items[0].answer == '可以'
    assert report.items[0].ttft_ms == 120.0


def test_quality_accepts_both_protocols() -> None:
    from relay_lab.config import QualityRequest

    for protocol in ('openai', 'openai_responses'):
        request = QualityRequest(url='http://127.0.0.1:1', model='m', authorized=True, protocol=protocol)
        assert request.protocol == protocol


def test_quality_responses_protocol_body_and_parse() -> None:
    """The Responses path targets /responses, sends `input`, and reads typed events."""
    import httpx

    captured: dict = {}
    events = [
        {'type': 'response.output_text.delta', 'delta': '可'},
        {'type': 'response.output_text.delta', 'delta': '以'},
        {'type': 'response.completed', 'response': {'usage': {'input_tokens': 5, 'output_tokens': 2}}},
    ]
    payload = ''.join(f'data: {json.dumps(event, ensure_ascii=False)}\n\n' for event in events) + 'data: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        captured['url'] = str(request.url)
        captured['body'] = json.loads(request.content)
        return httpx.Response(200, content=payload.encode(), headers={'content-type': 'text/event-stream'})

    item = quality.Item('指令', '只回复两个字：可以', '可以', 'exact')
    settings = QualitySettings(url='http://127.0.0.1:1/v1', model='m', protocol='openai_responses')

    async def call() -> quality.Answer:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await quality._call(client, settings, None, item, 10.0)

    answer = asyncio.run(call())
    assert captured['url'].endswith('/v1/responses')
    assert captured['body']['input'] == item.prompt
    assert 'messages' not in captured['body']
    assert answer.text == '可以'
    assert answer.error is None and answer.ttft_ms is not None


def test_quality_responses_protocol_reports_failed_event() -> None:
    import httpx

    payload = "data: " + json.dumps({'type': 'response.failed', 'response': {'error': {'code': 'rate_limit'}}}) + "\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload.encode(), headers={'content-type': 'text/event-stream'})

    item = quality.Item('指令', '只回复两个字：可以', '可以', 'exact')
    settings = QualitySettings(url='http://127.0.0.1:1/v1', model='m', protocol='openai_responses')

    async def call() -> quality.Answer:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await quality._call(client, settings, None, item, 10.0)

    answer = asyncio.run(call())
    assert answer.error is not None and 'rate_limit' in answer.error


# ---------- schedules ----------


def test_schedule_creation_persists_without_the_key(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)
    scheduler = Scheduler(tmp_path, lab)
    request = ScheduleRequest(
        label='每小时测速',
        kind='speed',
        interval_minutes=60,
        speed=SpeedSettings(url='http://127.0.0.1:19999', model='m', requests=3),
        api_key='sk-never-persisted',
    )
    schedule = scheduler.create(request)
    assert schedule.needs_key is False
    stored = (tmp_path / 'schedules.json').read_text(encoding='utf-8')
    assert 'sk-never-persisted' not in stored
    reloaded = Scheduler(tmp_path, lab)
    assert reloaded.list()[0].schedule_id == schedule.schedule_id
    assert reloaded.keys == {}


def test_schedule_without_key_reports_needs_key(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)
    scheduler = Scheduler(tmp_path, lab)
    schedule = scheduler.create(
        ScheduleRequest(
            label='无密钥',
            kind='speed',
            interval_minutes=30,
            speed=SpeedSettings(url='http://127.0.0.1:19999', model='m', requests=3),
        )
    )
    scheduler.run_now(schedule.schedule_id)
    assert scheduler.list()[0].needs_key is True
    assert scheduler.list()[0].last_status == 'needs_key'
    assert lab.history() == []


def test_due_schedule_fires_a_run(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)
    scheduler = Scheduler(tmp_path, lab)
    schedule = scheduler.create(
        ScheduleRequest(
            label='定时测速',
            kind='speed',
            interval_minutes=5,
            speed=SpeedSettings(url='http://127.0.0.1:19999', model='m', requests=2),
            api_key='sk-memory',
        )
    )
    schedule.next_run_at = '2020-01-01T00:00:00+00:00'
    with patch('relay_lab.search._execute', lambda *a, **k: native(total=2, succeeded=2)):
        scheduler.tick()
        runs = lab.history()
        assert runs
        assert lab.jobs[runs[0].run_id].done.wait(10)
    updated = scheduler.list()[0]
    assert updated.last_run_id == runs[0].run_id
    assert updated.next_run_at > '2026'


def test_api_kind_endpoints(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, schedule_loop=False))
    body = {'url': 'http://127.0.0.1:19999', 'model': 'm'}
    assert client.post('/api/speed', json={**body, 'requests': 3}).status_code == 400
    assert client.post('/api/parallel', json={**body, 'levels': [0], 'authorized': True}).status_code == 400
    # Both protocols are accepted now; the Responses path is built in quality._call.
    assert (
        client.post('/api/quality', json={**body, 'authorized': True, 'protocol': 'openai_responses'}).status_code
        == 202
    )

    schedule = client.post(
        '/api/schedules',
        json={
            'label': 'api',
            'kind': 'speed',
            'interval_minutes': 30,
            'speed': {'url': 'http://127.0.0.1:19999', 'model': 'm'},
        },
    )
    assert schedule.status_code == 201
    schedule_id = schedule.json()['schedule_id']
    assert client.get('/api/schedules').json()['schedules'][0]['schedule_id'] == schedule_id
    assert client.post(f'/api/schedules/{schedule_id}/toggle', json={'enabled': False}).json()['enabled'] is False
    assert client.delete(f'/api/schedules/{schedule_id}').status_code == 204
    assert client.get('/api/schedules').json()['schedules'] == []
    assert client.get('/api/health').json()['schedules'] == 0


def test_contract_endpoint_accepts_all_four_protocols(tmp_path: Path) -> None:
    """Each protocol must be reachable over HTTP, not only through the adapter."""
    client = TestClient(create_app(tmp_path, schedule_loop=False))
    for protocol in ('openai', 'openai_responses', 'anthropic', 'gemini'):
        response = client.post(
            '/api/contract',
            json={'url': 'http://127.0.0.1:19999', 'model': 'm', 'authorized': True, 'protocol': protocol},
        )
        assert response.status_code == 202, f'{protocol}: {response.status_code} {response.text}'
        assert response.json()['kind'] == 'contract'
        client.post(f"/api/runs/{response.json()['run_id']}/stop", json={})


def test_contract_endpoint_rejects_an_unknown_check(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, schedule_loop=False))
    response = client.post(
        '/api/contract',
        json={'url': 'http://127.0.0.1:19999', 'model': 'm', 'authorized': True, 'checks': ['tools', 'nope']},
    )
    assert response.status_code == 400
    assert 'nope' in response.json()['error']


def test_contract_run_records_verdicts_end_to_end(tmp_path: Path) -> None:
    """One real run through the lab, with the probe request stubbed to avoid a network."""
    lab = CapacityLab(tmp_path)
    reply = contract.protocols.Reply(text='{}', status=200, latency_ms=5.0)
    request = ContractRequest(
        url='http://127.0.0.1:19999', model='m', authorized=True, checks=['tools', 'errors']
    )
    with patch.object(contract, 'call', lambda *a, **k: reply):
        run = lab.start_contract(request)
        report = wait_done(lab, run.run_id)
    assert report.kind == 'contract'
    assert len(report.contract) == 2
    assert report.contract_summary is not None
    assert report.contract_summary.total == 2
    assert all(item.check in ('tools', 'errors') for item in report.contract)


# ---------- quality suite correctness ----------


def test_separator_item_accepts_the_chinese_comma_it_asks_for() -> None:
    """The prompt demands a Chinese comma; expecting an ASCII one made it unpassable."""
    item = next(entry for entry in quality.SUITE if '三个交通工具' in entry.prompt)
    assert quality.grade(item, '汽车，火车，飞机') is True
    assert quality.grade(item, '汽车,火车,飞机') is False


def test_json_item_rejects_a_number_where_a_boolean_was_asked() -> None:
    """True == 1 in Python, so an unchecked comparison would pass {'ok': 1}."""
    item = next(entry for entry in quality.SUITE if '字段 ok 的值' in entry.prompt)
    assert quality.grade(item, '{"ok": true}') is True
    assert quality.grade(item, '{"ok": 1}') is False


def test_unknown_category_is_rejected_instead_of_scoring_zero() -> None:
    with pytest.raises(ValidationError):
        QualitySettings(url='http://127.0.0.1:19999', model='m', categories=['算术', '编程'])


def test_every_item_can_pass_with_an_ideal_answer() -> None:
    """Guards the whole suite: any item that no correct answer can pass is a harness bug."""
    ideal = {
        '37 乘以 24': '计算过程：37 × 24 = 888',
        '240 的 15%': '36',
        '女生有多少人': '18',
        '先涨价 20%': '96',
        '从 1 加到 100': '5050',
        '5 个人需要几天': '3.6',
        '两管同时开': '2.4',
        '鸡兔同笼': '12',
        '只回复两个字': '可以',
        '转成大写': 'HELLO WORLD',
        '以“因为”开头': '因为瑞利散射，蓝光波长更短。',
        '三个交通工具': '汽车，火车，飞机',
        '倒序输出': 'yaler',
        '中文数字按顺序': '一、二、三、四、五',
        '改写成被动句': '项目被团队完成。',
        '转成二进制': '101010',
        '字段 ok 的值为 true': '{"ok": true}',
        '字段 name 的值为 relay': '{"name": "relay"}',
        '包含 1、2、3 三个数字': '[1, 2, 3]',
        'msg 等于 ready': '{"code": 200, "msg": "ready"}',
        '嵌套对象 user': '{"user": {"id": 7, "vip": false}}',
        '两个元素都是对象': '[{"id": 1, "ok": true}, {"id": 2, "ok": false}]',
        'tags 是': '{"tags": ["a", "b", "c"], "count": 3}',
        '一个双引号': '{"text": "a\\"b"}',
        '飞往成都': '成都',
        '找出这句话的主语': '小猫',
        '改成否定句': '他今天不会来。',
        '翻译成英文': 'It is raining today.',
        '“她”指的是谁': '小红',
        '他不是不想去': '去',
        '不用“虽然…但是”': '比赛仍然照常进行。',
        '画蛇添足': '画蛇添足比喻做了多余的事。',
        '函数 add(a, b)': 'def add(a, b):\n    return a + b',
        '打印 1 到 5': 'for i in range(1, 6):\n    print(i)',
        'i * i for i in range(3)': '它把 0 到 2 分别平方，得到 0, 1, 4。',
        '选出 name 列': 'SELECT name FROM users;',
        'is_even(n)': 'def is_even(n):\n    return n % 2 == 0',
        'age 大于 18': 'SELECT name FROM users WHERE age > 18 ORDER BY name;',
        '结果是错的': '少除了元素个数（长度），所以没有求出平均值。',
        '递归函数 fact(n)': 'def fact(n):\n    if n <= 1:\n        return 1\n    return n * fact(n - 1)',
        '苏格拉底会死吗': '会',
        '数列 2、4、8、16': '32',
        '9 个球里有一个偏重': '2',
        '只有一个人说真话': '乙',
        '并非所有人都及格': '有人没及格',
        '至少出现一个正面': '3/4',
        '丁在丙后面': '乙',
        '标签全错': '1',
        '通行码数字': '5827',
        '几个圆形': '3',
        '最高的那根柱子': '3',
        '向左还是向右移动': '右',
    }
    seen: dict[str, int] = {}
    for item in quality.SUITE:
        seen[item.category] = seen.get(item.category, 0) + 1
        matches = [answer for prompt, answer in ideal.items() if prompt in item.prompt]
        assert len(matches) == 1, f'理想答案缺失或重复：{item.prompt}'
        assert quality.grade(item, matches[0]) is True, item.prompt
    assert seen == {'算术': 8, '指令': 8, 'JSON': 8, '中文理解': 8, '代码': 8, '逻辑推理': 8, '多模态': 4}
    assert set(ideal) == {prompt for prompt in ideal}, 'ideal 表本身有重复键'
