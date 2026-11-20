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
    LoadRequest,
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
from tests.conftest import login


def _half(value):
    """Keep the None case intact for the missing-metric tests."""
    return None if value is None else value / 2


def native(*, succeeded=5, total=5, seconds=5, ttft=100.0, tpot=20.0) -> tuple:
    return (
        BenchmarkSummary(
            time_taken=seconds,
            total_requests=total,
            succeed_requests=succeeded,
            stream_requests=succeeded,
            avg_input_tokens=24,
            avg_output_tokens=16,
            avg_latency=0.5,
        ),
        PercentileResult(
            rows=[
                PercentileRow(percentile='50%', ttft=_half(ttft), tpot=_half(tpot), latency=0.4),
                PercentileRow(percentile='90%', ttft=ttft, tpot=tpot, latency=0.7),
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
    assert stage.latency_p50_ms == 400
    assert stage.latency_p90_ms == 700
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
    item = next(entry for entry in quality.PACKS['smoke'] if prompt_part in entry.prompt)
    assert quality.grade(item, answer) is expected


def test_suite_selection_respects_category_and_count() -> None:
    settings = QualitySettings(url='http://127.0.0.1:1', model='m', pack='smoke', categories=['算术'], per_category=2)
    chosen = quality.select(settings)
    assert len(chosen) == 2
    assert {item.category for item in chosen} == {'算术'}
    assert len(quality.select(QualitySettings(url='http://127.0.0.1:1', model='m', pack='smoke', per_category=20))) == len(
        quality.PACKS['smoke']
    )
    assert len(quality.select(QualitySettings(url='http://127.0.0.1:1', model='m', pack='standard', per_category=20))) == len(
        quality.PACKS['standard']
    )


def test_quality_run_fills_items_and_summary(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)

    async def fake_call(client, settings, api_key, item, timeout):
        return quality.Answer(text='可以' if '只回复两个字' in item.prompt else '', ttft_ms=120.0, latency_ms=300.0, tokens=4)

    request = QualityRequest(
        url='http://127.0.0.1:19999',
        model='m',
        authorized=True,
        pack='smoke',
        categories=['指令'],
        per_category=1,
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
    login(client)
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
    login(client)
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
    login(client)
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
    # 2 checks × 3 fixed protocols
    assert len(report.contract) == 6
    assert report.contract_summary is not None
    assert report.contract_summary.total == 6
    assert all(item.check in ('tools', 'errors') for item in report.contract)
    assert {item.protocol for item in report.contract} == {'openai', 'openai_responses', 'anthropic'}


def test_contract_repeats_persist_actual_requests_including_skips(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)
    reply = contract.protocols.Reply(text='{}', status=200, latency_ms=5.0)
    request = ContractRequest(
        url='http://127.0.0.1:19999', model='m', authorized=True,
        checks=['dynamic_tools', 'tool_roundtrip'], per_check=2,
    )
    with patch.object(contract, 'call', lambda *a, **k: reply):
        run = lab.start_contract(request)
        report = wait_done(lab, run.run_id)
    # dynamic_tools 仅适用于 Chat Completions（model='m' 不支持时仍记 0 请求跳过）；tool_roundtrip 覆盖全部 3 协议
    assert report.contract_summary.total == 8
    assert [item.repeat for item in report.contract] == [1, 2, 1, 2, 1, 2, 1, 2]
    assert [item.requests for item in report.contract] == [0, 0, 1, 1, 1, 1, 1, 1]
    assert report.reserved_requests == 6


def test_presets_expose_contract_modes_and_fast_mode_runs_single_wire(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, schedule_loop=False))
    login(client)
    presets = client.get('/api/presets').json()
    assert set(presets['contract_modes']) == {'fast', 'full'}
    assert 'cache_roundtrip' not in presets['contract_modes']['fast']['checks']
    assert 'stream_tools' in presets['contract_modes']['full']['checks']

    response = client.post(
        '/api/contract',
        json={
            'url': 'http://127.0.0.1:19999',
            'model': 'kimi-k3.1-pro',
            'protocol': 'openai',
            'mode': 'fast',
            'authorized': True,
        },
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload['settings']['mode'] == 'fast'
    assert any('快速模式' in warning for warning in payload['warnings'])
    client.post(f"/api/runs/{payload['run_id']}/stop", json={})


def test_contract_report_export_includes_protocol_breakdown_and_wire_evidence(tmp_path: Path) -> None:
    app = create_app(tmp_path, schedule_loop=False)
    client = TestClient(app)
    login(client)
    reply = contract.protocols.Reply(
        text='{"ok": false}',
        status=200,
        latency_ms=42.0,
        ttft_ms=15.0,
        headers={'x-request-id': 'req_123'},
        request_headers={'Authorization': 'Bearer sk-***'},
        raw_body='{"choices":[{"message":{"content":"hello"}}]}',
    )
    with patch.object(contract, 'call', lambda *a, **k: reply):
        response = client.post(
            '/api/contract',
            json={
                'url': 'http://127.0.0.1:19999',
                'model': 'kimi-k3.1-pro',
                'authorized': True,
                'checks': ['tools', 'errors'],
            },
        )
        assert response.status_code == 202
        run_id = response.json()['run_id']
        wait_done(app.state.lab, run_id)

    exported = client.get(f'/api/runs/{run_id}/report')
    assert exported.status_code == 200
    assert 'text/markdown' in exported.headers['content-type']
    md = exported.text
    assert '## 分协议汇总' in md
    assert 'Chat Completions (openai)' in md
    assert 'Responses (openai_responses)' in md
    assert 'Anthropic Messages (anthropic)' in md
    assert '## 重点问题与异常诊断' in md
    assert '<details>' in md
    assert '| 序号 | 协议 | 检查项 | 严重级别 | 结论 | 耗时 | 应该返回 | 实际返回 | 判定说明 |' in md


# ---------- quality suite correctness ----------


def test_separator_item_accepts_the_chinese_comma_it_asks_for() -> None:
    """The prompt demands a Chinese comma; expecting an ASCII one made it unpassable."""
    item = next(entry for entry in quality.PACKS['smoke'] if '三个交通工具' in entry.prompt)
    assert quality.grade(item, '汽车，火车，飞机') is True
    assert quality.grade(item, '汽车,火车,飞机') is False


def test_json_item_rejects_a_number_where_a_boolean_was_asked() -> None:
    """True == 1 in Python, so an unchecked comparison would pass {'ok': 1}."""
    item = next(entry for entry in quality.PACKS['smoke'] if '字段 ok 的值' in entry.prompt)
    assert quality.grade(item, '{"ok": true}') is True
    assert quality.grade(item, '{"ok": 1}') is False


def test_unknown_category_is_rejected_instead_of_scoring_zero() -> None:
    with pytest.raises(ValidationError):
        QualitySettings(url='http://127.0.0.1:19999', model='m', categories=['算术', '编程'])
    with pytest.raises(ValidationError):
        QualitySettings(url='http://127.0.0.1:19999', model='m', pack='standard', categories=['算术'])


def test_every_item_can_pass_with_an_ideal_answer() -> None:
    """Guards every pack: any item that no correct answer can pass is a harness bug."""
    # MMLU standard pack: the expected letter is the ideal answer (plus common wrappers).
    for item in quality.PACKS['standard']:
        assert quality.grade(item, item.expected) is True, item.source
        assert quality.grade(item, f'Answer: {item.expected}') is True, item.source
        assert quality.grade(item, 'Z') is False, item.source
    assert {item.category for item in quality.PACKS['standard']} == {'STEM', '人文', '社科', '其他'}

    ideals: dict[str, dict[str, str]] = {
        'smoke': {
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
        },
    }
    expected_counts = {
        'smoke': {'算术': 8, '指令': 8, 'JSON': 8, '中文理解': 8, '代码': 8, '逻辑推理': 8, '多模态': 4},
    }
    for pack_name, ideal in ideals.items():
        seen: dict[str, int] = {}
        for item in quality.PACKS[pack_name]:
            seen[item.category] = seen.get(item.category, 0) + 1
            matches = [answer for prompt, answer in ideal.items() if prompt in item.prompt]
            assert len(matches) == 1, f'[{pack_name}] 理想答案缺失或重复：{item.prompt}'
            assert quality.grade(item, matches[0]) is True, f'[{pack_name}] {item.prompt}'
        assert seen == expected_counts[pack_name], pack_name


def write_load_log(arguments, *batches: tuple[int, int, float]) -> None:
    import sqlite3

    db = Path(arguments.outputs_dir) / 'benchmark_data.db'
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as con:
        con.execute(
            'create table result (start_time real, completed_time real, success integer, '
            'prompt_tokens integer, completion_tokens integer, error text, status_code integer)'
        )
        con.executemany(
            'insert into result values (0, ?, ?, 24, 16, null, null)',
            ((finished, int(i < succeeded)) for total, succeeded, finished in batches for i in range(total)),
        )


def test_load_mode_ramps_concurrency_and_counts_success(tmp_path: Path) -> None:
    """Each window runs flat out; the reported peak is a throughput number."""
    lab = CapacityLab(tmp_path)
    calls = []

    def fake(arguments, task_id, timeout):
        calls.append(arguments)
        level = arguments.parallel[0]
        write_load_log(arguments, (level * 5, level * 5, 10))
        return native(total=level * 5, succeeded=level * 5, seconds=10, ttft=200)

    request = LoadRequest(
        url='http://127.0.0.1:19999', model='m', authorized=True,
        concurrency_start=4, concurrency_step=8, windows=3, window_seconds=10, max_requests=1000,
    )
    with patch('relay_lab.search._execute', fake):
        run = lab.start_load(request)
        report = wait_done(lab, run.run_id)

    assert report.kind == 'load'
    assert [stage.pressure for stage in report.stages] == [4, 12, 20]
    assert all(stage.phase == 'load' and stage.status == 'passed' for stage in report.stages)
    # Unpaced: no rate limit, one semaphore slot per concurrency level.
    assert all(call.rate[0] == -1 for call in calls)
    assert all(call.warmup_num == 0 for call in calls)
    # Supply the whole remaining budget, without an artificial per-window estimate.
    assert [call.number[0] for call in calls] == [1000, 980, 920]
    assert all(call.duration == 10 for call in calls)
    assert report.load is not None and report.load.peak_concurrency == 20
    assert report.reserved_requests == sum(stage.requests for stage in report.stages)


def test_load_mode_drops_windows_below_the_success_threshold(tmp_path: Path) -> None:
    """A window that mostly failed must not become the reported peak."""
    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        level = arguments.parallel[0]
        succeeded = level * 5 if level <= 4 else level
        write_load_log(arguments, (level * 5, succeeded, 10))
        return native(total=level * 5, succeeded=succeeded, seconds=10, ttft=200)

    request = LoadRequest(
        url='http://127.0.0.1:19999', model='m', authorized=True,
        concurrency_start=4, concurrency_step=8, windows=2, window_seconds=10,
    )
    with patch('relay_lab.search._execute', fake):
        run = lab.start_load(request)
        report = wait_done(lab, run.run_id)

    assert [stage.status for stage in report.stages] == ['passed', 'failed']
    assert report.load is not None and report.load.peak_concurrency == 4
    assert '低于' in report.stages[1].reason


def test_short_window_is_not_reported_as_the_peak(tmp_path: Path) -> None:
    """A window that ended early covers less than its stated duration.

    Its rate is arithmetically real but says nothing about "per minute at this
    concurrency", so it must be labelled instead of silently winning the comparison.
    """
    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        write_load_log(arguments, (5, 5, 3))
        return native(total=5, succeeded=5, seconds=3, ttft=200)

    request = LoadRequest(
        url='http://127.0.0.1:19999', model='m', authorized=True,
        concurrency_start=8, concurrency_step=0, windows=1, window_seconds=10,
    )
    with patch('relay_lab.search._execute', fake):
        run = lab.start_load(request)
        report = wait_done(lab, run.run_id)

    assert report.stages[0].status == 'unknown'
    assert '不计入峰值' in report.stages[0].reason
    assert report.load is not None and report.load.peak_concurrency is None


def test_throughput_is_counted_inside_the_window_not_over_total_runtime(tmp_path: Path) -> None:
    """A 10 s window that takes 46 s to drain still means 10 s of throughput."""
    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        # The engine reports the drain-inclusive duration, which must not be the divisor.
        write_load_log(arguments, (44, 44, 10), (16, 16, 46))
        return native(total=60, succeeded=60, seconds=46, ttft=5400)

    request = LoadRequest(
        url='http://127.0.0.1:19999', model='m', authorized=True,
        concurrency_start=36, concurrency_step=0, windows=1, window_seconds=10,
        ttft_p99_ms=10000,  # this fixture measures window RPM, not TTFT SLA
    )
    with patch('relay_lab.search._execute', fake):
        run = lab.start_load(request)
        report = wait_done(lab, run.run_id)

    stage = report.stages[0]
    assert stage.rpm == 264.0, '44 completions inside 10 s is 264 per minute'
    assert stage.window_requests == stage.window_succeeded == 44
    assert stage.window_success_percent == 100
    assert stage.output_tpm == 44 * 16 * 6
    assert '窗口内完成 44' in stage.reason
    assert report.load is not None and report.load.peak_rpm == 264.0
    assert report.load.suggested_rpm == round(264.0 * 0.7, 1)


def test_stopped_load_keeps_measured_window_and_percentiles(tmp_path: Path) -> None:
    import sqlite3
    from relay_lab.runner import ProbeStopped

    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        write_load_log(arguments, (2, 2, 2), (1, 1, 4))
        with sqlite3.connect(Path(arguments.outputs_dir) / 'benchmark_data.db') as con:
            for column, value in [('latency', 0.5), ('first_chunk_latency', 0.2),
                                  ('time_per_output_token', 0.02), ('is_stream', 1)]:
                con.execute(f'alter table result add column {column} real default {value}')
        lab.stop(task_id.rsplit('-', 1)[0])
        raise ProbeStopped()

    request = LoadRequest(url='http://127.0.0.1:1', model='m', authorized=True,
                          concurrency_start=2, window_seconds=5, max_requests=10)
    with patch('relay_lab.search._execute', fake):
        report = wait_done(lab, lab.start_load(request).run_id)
    stage = report.stages[0]
    assert report.status == 'stopped' and stage.partial_results and stage.status == 'unknown'
    assert (stage.requests, stage.window_requests, stage.window_success_percent) == (3, 3, 100)
    assert stage.window_rate_seconds == 4
    assert (stage.rpm, stage.output_tpm) == (45, 720)
    assert (stage.ttft_p95_ms, stage.latency_p99_ms, stage.tpot_p95_ms) == (200, 500, 20)
    assert report.load and report.load.peak_rpm is None and report.load.suggested_rpm is None


def test_load_budget_burst_cannot_pass_by_draining_past_deadline(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        write_load_log(arguments, (9, 9, 0.25), (1, 1, 5.6))
        return native(total=10, succeeded=10, seconds=5.6)

    request = LoadRequest(url='http://127.0.0.1:1', model='m', authorized=True, stream=False,
                          concurrency_start=10, max_requests=10, window_seconds=5)
    with patch('relay_lab.search._execute', fake):
        report = wait_done(lab, lab.start_load(request).run_id)
    assert report.stages[0].rpm == 108
    assert report.stages[0].status == 'unknown' and '预算' in report.stages[0].reason
    assert report.load and report.load.peak_rpm is None and report.load.suggested_rpm is None


@pytest.mark.parametrize('source,tpot', [('reported', None), ('estimated', 20), ('missing', None)])
def test_load_requires_verifiable_stream_tpot(tmp_path: Path, source: str, tpot: float | None) -> None:
    import sqlite3

    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        write_load_log(arguments, (5, 5, 5))
        with sqlite3.connect(Path(arguments.outputs_dir) / 'benchmark_data.db') as con:
            con.execute('alter table result add column token_source text')
            con.execute('update result set token_source = ?', (source,))
        return native(total=5, succeeded=5, seconds=5, tpot=tpot)

    request = LoadRequest(url='http://127.0.0.1:1', model='m', authorized=True,
                          concurrency_start=2, max_requests=10, window_seconds=5)
    with patch('relay_lab.search._execute', fake):
        report = wait_done(lab, lab.start_load(request).run_id)
    stage = report.stages[0]
    assert (stage.status, stage.token_source, stage.rpm) == ('unknown', source, 60)
    assert 'TPOT' in stage.reason
    assert stage.output_tpm == (None if source == 'missing' else 960)
    assert report.load and report.load.suggested_rpm is None


def test_load_window_success_rate_excludes_late_successes(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        write_load_log(arguments, (10, 1, 9), (90, 90, 15))
        return native(total=100, succeeded=91, seconds=15)

    request = LoadRequest(
        url='http://127.0.0.1:19999', model='m', authorized=True,
        concurrency_start=100, window_seconds=10,
    )
    with patch('relay_lab.search._execute', fake):
        report = wait_done(lab, lab.start_load(request).run_id)

    stage = report.stages[0]
    assert (stage.window_succeeded, stage.window_requests, stage.window_success_percent) == (1, 10, 10)
    assert (stage.succeeded, stage.requests, stage.success_percent) == (91, 100, 91)
    assert stage.status == 'failed'
    assert '窗口成功率 10.0%' in stage.reason
    assert stage.rpm == 6
    assert report.reserved_requests == 100
    assert report.load is not None and report.load.suggested_rpm is None


@pytest.mark.parametrize('log_state', ['missing', 'unreadable', 'incomplete'])
@pytest.mark.parametrize('earlier_peak', [False, True])
def test_load_unverified_window_cannot_set_peak(tmp_path: Path, log_state: str, earlier_peak: bool) -> None:
    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        if earlier_peak and arguments.parallel[0] == 4:
            write_load_log(arguments, (20, 20, 10))
            return native(total=20, succeeded=20, seconds=10)
        if log_state == 'unreadable':
            path = Path(arguments.outputs_dir) / 'benchmark_data.db'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'not a database')
        if log_state == 'incomplete':
            write_load_log(arguments, (9, 9, 10))
        return native(total=10, succeeded=10, seconds=1)

    request = LoadRequest(
        url='http://127.0.0.1:19999', model='m', authorized=True,
        concurrency_start=4, concurrency_step=4, window_seconds=10, windows=2 if earlier_peak else 1,
    )
    with patch('relay_lab.search._execute', fake):
        report = wait_done(lab, lab.start_load(request).run_id)

    stage = report.stages[-1]
    assert stage.status == 'unknown' and '无法核实窗口数据' in stage.reason
    assert stage.window_requests is None and stage.window_success_percent is None
    assert report.load is not None
    assert report.load.peak_rpm == (120 if earlier_peak else None)
    assert report.load.suggested_rpm == (84 if earlier_peak else None)


def test_load_mode_defaults_to_constant_concurrency() -> None:
    """快速吞吐 defaults match the console's 恒定并发 profile."""
    settings = LoadRequest(url='http://127.0.0.1:1', model='m', authorized=True)
    assert settings.concurrency_start == 100
    assert settings.concurrency_step == 0
    assert settings.window_seconds == 60
    assert settings.windows == 1
    assert settings.success_percent == 90
    assert settings.circuit_success_percent == 80
    assert settings.max_consecutive_failures == 20
    assert settings.avg_latency_ms == 150000
    assert settings.ttft_p99_ms == 3000
    assert settings.tpot_p99_ms == 50
    assert settings.capacity_ratio == 0.7


def test_load_circuit_trips_on_success_rate(tmp_path: Path) -> None:
    """Success below the circuit threshold ends the window as overload, not SLA."""
    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        write_load_log(arguments, (100, 90, 10))
        return native(total=100, succeeded=90, seconds=10, ttft=200)

    request = LoadRequest(
        url='http://127.0.0.1:19999', model='m', authorized=True,
        concurrency_start=4, concurrency_step=0, windows=1, window_seconds=10,
        circuit_success_percent=95, success_percent=90,
    )
    with patch('relay_lab.search._execute', fake):
        run = lab.start_load(request)
        report = wait_done(lab, run.run_id)

    assert report.stages[0].status == 'failed'
    assert '熔断' in report.stages[0].reason
    assert report.load is not None and report.load.peak_concurrency is None
    assert '本窗实测' in report.message
    assert '不计入峰值' in report.message


@pytest.mark.parametrize(('step', 'expected_windows', 'saturated'), [(8, 2, True), (0, 5, False)])
def test_load_saturation_requires_more_concurrency(
    tmp_path: Path, step: int, expected_windows: int, saturated: bool,
) -> None:
    """Flat throughput signals saturation only when the pressure actually increased."""
    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        # Identical throughput at every level: the plateau must be detected.
        write_load_log(arguments, (50, 50, 10))
        return native(total=50, succeeded=50, seconds=10, ttft=200)

    request = LoadRequest(
        url='http://127.0.0.1:19999', model='m', authorized=True,
        concurrency_start=4, concurrency_step=step, windows=5, window_seconds=10,
    )
    with patch('relay_lab.search._execute', fake):
        run = lab.start_load(request)
        report = wait_done(lab, run.run_id)

    assert len(report.stages) == expected_windows
    assert report.load is not None and report.load.saturated is saturated
    assert ('饱和' in (report.load.stop_reason or '')) is saturated


def test_load_mode_stops_after_a_failed_window(tmp_path: Path) -> None:
    """Climbing past a window that already missed the success target only wastes budget."""
    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        level = arguments.parallel[0]
        succeeded = level * 5 if level <= 4 else 1
        write_load_log(arguments, (level * 5, succeeded, 10))
        return native(total=level * 5, succeeded=succeeded, seconds=10, ttft=200)

    request = LoadRequest(
        url='http://127.0.0.1:19999', model='m', authorized=True,
        concurrency_start=4, concurrency_step=8, windows=5, window_seconds=10,
        circuit_success_percent=0, max_consecutive_failures=0,
    )
    with patch('relay_lab.search._execute', fake):
        run = lab.start_load(request)
        report = wait_done(lab, run.run_id)

    assert len(report.stages) == 2
    assert report.load is not None and '未达标' in (report.load.stop_reason or '')


def test_load_rejects_unsupported_protocol_and_excessive_concurrency() -> None:
    with pytest.raises(ValidationError):
        LoadRequest(url='http://127.0.0.1:1', model='m', protocol='anthropic', authorized=True)
    with pytest.raises(ValidationError):
        LoadRequest(
            url='http://127.0.0.1:1', model='m', authorized=True,
            concurrency_start=200, concurrency_step=50, windows=3,
        )


def test_nonstream_observe_clears_tpot_and_openai_usage_fallback() -> None:
    from relay_lab import engine
    from relay_lab.config import LabStage
    from relay_lab.vendor.perf.arguments import Arguments
    from relay_lab.vendor.perf.plugin.api.openai_api import OpenaiPlugin

    stage = LabStage(index=1, phase='load', pressure=4, repeat=1, native_task_id='t')
    res = (
        BenchmarkSummary(
            time_taken=10, total_requests=5, succeed_requests=5, stream_requests=0,
            avg_input_tokens=10, avg_output_tokens=20, avg_latency=0.5,
        ),
        PercentileResult(rows=[PercentileRow(percentile='99%', ttft=0.5, tpot=0.0, latency=0.6)]),
        None,
        None,
    )
    obs = engine.observe(stage, res)
    assert obs.tpot_p99_ms is None
    assert obs.ttft_p99_ms is None

    plugin = OpenaiPlugin(Arguments(model='m', stream=False))
    in_tok, out_tok = plugin.parse_responses(
        [{'choices': [{'message': {'content': 'hello world test'}}]}],
        request='{"messages":[{"role":"user","content":"hi there"}]}',
    )
    assert in_tok > 0 and out_tok > 0


def test_closed_loop_cancels_queued_tasks_when_duration_expires() -> None:
    from relay_lab.vendor.perf.arguments import Arguments
    from relay_lab.vendor.perf.core.strategies.closed_loop import ClosedLoopStrategy
    from relay_lab.vendor.perf.utils.benchmark_util import BenchmarkData

    sent = 0

    class SlowClient:
        async def post(self, request):
            nonlocal sent
            sent += 1
            await asyncio.sleep(0.15)
            return BenchmarkData(success=True, start_time=1.0, completed_time=1.15)

    async def gen():
        for i in range(50):
            yield {'i': i}, False

    async def run_case():
        args = Arguments(model='m', number=50, parallel=2, duration=0.1, rate=-1)
        args.number = 50
        args.parallel = 2
        args.rate = -1
        q: asyncio.Queue = asyncio.Queue()
        strat = ClosedLoopStrategy(args, None, SlowClient(), q, gen())  # type: ignore[arg-type]
        await strat.run()
        return q.qsize()

    finished = asyncio.run(run_case())
    # parallel=2、duration=0.1s、单请求 0.15s，最多只会发出最初占满 2 槽位的请求，绝不会把排队的第 3、4 个也发出去。
    assert sent <= 2
    assert finished == sent


def test_live_circuit_breaker_reads_consecutive_failures(tmp_path: Path) -> None:
    import sqlite3

    from relay_lab.config import LoadSettings
    from relay_lab.search import _load_circuit_reason

    db = tmp_path / 'benchmark_data.db'
    with sqlite3.connect(db) as con:
        con.execute('create table result (success integer, latency real, start_time real, completed_time real)')
        for i in range(25):
            con.execute('insert into result values (?, ?, ?, ?)', (0, 0.1, float(i), float(i) + 0.1))
        con.commit()

    settings = LoadSettings(url='http://127.0.0.1:1', model='m', max_consecutive_failures=20)
    reason = _load_circuit_reason(db, settings)
    assert reason is not None and '连续失败' in reason


def test_default_api_nonstream_rejects_http_200_error_envelope() -> None:
    from relay_lab.vendor.perf.arguments import Arguments
    from relay_lab.vendor.perf.plugin.api.default_api import DefaultApiPlugin

    class Response:
        status = 200
        headers = {'Content-Type': 'application/json'}
        reason = 'OK'

        async def json(self):
            return {'error': {'message': 'quota exceeded', 'type': 'insufficient_quota'}}

    class Context:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *_):
            return False

    class Session:
        def post(self, **_):
            return Context()

    plugin = DefaultApiPlugin(Arguments(model='m', stream=False))
    output = asyncio.run(plugin.process_request(Session(), 'https://site.test/v1/chat/completions', {}, {'stream': False}))
    assert output.success is False
    assert 'insufficient_quota' in (output.error or '')


def test_load_zero_window_completions_reports_zero_rpm_and_tpm(tmp_path: Path) -> None:
    """所有请求都在窗口外才完成时，窗口内 RPM/TPM 必须记为 0，不能跳过重算。"""
    import sqlite3

    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        db = Path(arguments.outputs_dir) / 'benchmark_data.db'
        db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db) as con:
            con.execute(
                'create table result (start_time real, completed_time real, latency real, '
                'success integer, prompt_tokens integer, completion_tokens integer)'
            )
            # 10s 窗口，但全部请求都在第 15s 才完成。
            for _ in range(4):
                con.execute('insert into result values (?, ?, ?, ?, ?, ?)', (0.0, 15.0, 15.0, 1, 100, 50))
            con.commit()
        return native(total=4, succeeded=4, seconds=15, ttft=200)

    request = LoadRequest(
        url='http://127.0.0.1:19999', model='m', authorized=True,
        concurrency_start=4, concurrency_step=0, windows=1, window_seconds=10,
    )
    with patch('relay_lab.search._execute', fake):
        run = lab.start_load(request)
        report = wait_done(lab, run.run_id)

    stage = report.stages[0]
    assert stage.rpm == 0.0
    assert stage.output_tpm == 0.0
    assert stage.status == 'unknown'
    assert stage.window_requests == stage.window_succeeded == 0
    assert stage.window_success_percent is None
    assert report.load is not None
    assert report.load.peak_concurrency is None
    assert report.load.peak_rpm is None
    assert report.load.suggested_rpm is None
    assert '窗口内完成 0 个' in stage.reason
