"""Behaviour tests for the lab's own controller; no network and no credentials."""

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from relay_lab import engine, store
from relay_lab.api import create_app
from relay_lab.config import LabRun, LabSettings, LabStage, LoadSettings, SpeedSettings, StartRequest
from relay_lab.engine import summarize
from relay_lab.search import CapacityLab, _load_verdict, fingerprint, required_tokens
from relay_lab.vendor.perf.utils.perf_models import BenchmarkSummary, PercentileResult, PercentileRow
from tests.conftest import login


def config(**kwargs) -> StartRequest:
    """Return a bounded request targeting only loopback."""
    defaults = dict(url='http://127.0.0.1:19999', model='local-fixture', authorized=True, cooldown_seconds=0)
    return StartRequest(**(defaults | kwargs))


def _half(value):
    """Keep the None case intact for the missing-metric tests."""
    return None if value is None else value / 2


def native(*, succeeded=10, total=10, seconds=10, ttft=100.0, tpot=20.0) -> tuple:
    """Mimic one load-generator result without starting a process."""
    return (
        BenchmarkSummary(
            time_taken=seconds,
            total_requests=total,
            succeed_requests=succeeded,
            stream_requests=succeeded,
            avg_input_tokens=24,
            avg_output_tokens=16,
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


def stage(phase='rpm') -> LabStage:
    return LabStage(index=1, phase=phase, pressure=60, repeat=1, native_task_id='local-test')


def wait_done(lab: CapacityLab, run_id: str):
    assert lab.jobs[run_id].done.wait(10)
    return next(item for item in lab.history() if item.run_id == run_id)


# ---------- summarising ----------


def test_nonstream_load_does_not_require_first_byte() -> None:
    settings = LoadSettings(url='http://127.0.0.1:19999', model='m', stream=False)
    observed = LabStage(index=1, phase='load', pressure=4, repeat=1, native_task_id='t')
    observed.requests = 8
    observed.succeeded = 8
    observed.success_percent = 100
    observed.window_requests = 8
    observed.window_succeeded = 8
    observed.window_success_percent = 100
    observed.latency_avg_ms = 400
    verdict = _load_verdict(observed, settings)
    assert verdict.status == 'passed'
    assert engine.build(
        model='m', url='http://127.0.0.1:19999', protocol='openai', api_key=None,
        dataset_path='p', outputs_dir='o', number=1, parallel=1, rate=-1, duration=5,
        max_tokens=16, timeout=30, seed=1, stream=False,
    ).stream is False


def test_minute_units_and_distinct_total_tpm() -> None:
    observed = summarize(stage('tpm'), native(), config())
    assert observed.rpm == 60
    assert observed.input_tpm == 1440
    assert observed.output_tpm == 960
    assert observed.total_tpm == 2400
    assert observed.latency_p50_ms == 400
    assert observed.latency_p90_ms == 700
    assert observed.status == 'passed'


def test_success_denominator_includes_failures() -> None:
    observed = summarize(stage(), native(succeeded=5), config())
    assert observed.rpm == 30
    assert observed.success_percent == 50
    assert observed.status == 'failed'


@pytest.mark.parametrize('missing', ['ttft', 'tpot'])
def test_missing_latency_cannot_pass(missing: str) -> None:
    assert summarize(stage('tpm'), native(**{missing: None}), config()).status == 'unknown'


def test_high_ttft_fails_without_a_second_breaker() -> None:
    observed = summarize(stage(), native(ttft=4000), config())
    assert observed.status == 'failed'
    assert 'TTFT' in observed.reason


def test_invalid_native_counters_and_duration_fail_closed() -> None:
    with pytest.raises(ValueError):
        summarize(stage(), native(succeeded=11, total=10), config())
    with pytest.raises(ValueError):
        summarize(stage(), native(seconds=0), config())


# ---------- validation ----------


@pytest.mark.parametrize(
    'url',
    [
        'file:///tmp/test',
        'http://user:secret@localhost',
        'https://example.test/?key=secret',
        'https://example.test/#secret',
    ],
)
def test_urls_cannot_embed_credentials_or_local_files(url: str) -> None:
    with pytest.raises(ValidationError):
        config(url=url)


def test_authorization_and_consistent_bounds_required() -> None:
    with pytest.raises(ValidationError):
        StartRequest(url='http://localhost:1', model='x', authorized=False)
    with pytest.raises(ValidationError):
        config(start_rpm=100, max_rpm=10)
    with pytest.raises(ValidationError):
        config(max_parallel=129)


@pytest.mark.parametrize('value', [False, 1, 'true', None])
def test_authorization_requires_a_boolean_true(value: object) -> None:
    with pytest.raises(ValidationError):
        config(authorized=value)


def test_protocol_and_explicit_endpoint_must_match() -> None:
    with pytest.raises(ValidationError):
        config(url='https://example.test/v1/responses', protocol='openai')
    with pytest.raises(ValidationError):
        config(url='https://example.test/v1/chat/completions', protocol='openai_responses')


def test_fingerprint_ignores_target_and_label_but_not_sla() -> None:
    first = LabSettings(url='https://a.example', model='same')
    second = first.model_copy(update={'url': 'https://b.example', 'label': 'another'})
    assert fingerprint(first) == fingerprint(second)
    assert fingerprint(first) != fingerprint(second.model_copy(update={'success_percent': 100}))


# ---------- search behaviour ----------


def test_budget_is_global_and_the_key_is_never_persisted(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)
    calls = []

    def fake(arguments, task_id, timeout):
        calls.append(arguments)
        return native(total=arguments.number[0], succeeded=arguments.number[0])

    with patch('relay_lab.search._execute', fake):
        run = lab.start(
            config(
                api_key='mock-secret-never-export', max_requests=40, max_steps=2, refine_steps=0, token_reserve=100000
            )
        )
        report = wait_done(lab, run.run_id)
    assert report.reserved_requests <= 40
    assert calls
    assert all(call.url.endswith('/v1/chat/completions') for call in calls)
    for path in (tmp_path / run.run_id).glob('stage-*/baseline-args.json'):
        assert 'mock-secret-never-export' not in path.read_text(encoding='utf-8')
    exported = (tmp_path / run.run_id / 'experiment.json').read_text(encoding='utf-8')
    assert 'mock-secret-never-export' not in exported
    assert 'api_key' not in exported


def test_first_stage_failure_halves_down_to_a_passing_floor(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        return native(ttft=4000 if arguments.rate[0] >= 1.5 else 100)

    with patch('relay_lab.search._execute', fake):
        run = lab.start(config(goal='rpm', start_rpm=120, max_rpm=480, max_requests=200, max_steps=3, refine_steps=0))
        report = wait_done(lab, run.run_id)
    pressures = [row.pressure for row in report.stages]
    assert pressures[:2] == [120, 60]
    assert [report.stages[0].status, report.stages[1].status] == ['failed', 'passed']
    assert report.findings[0].status == 'bounded'
    assert report.findings[0].pressure == 60
    assert report.findings[0].value == 42.0  # measured 60 RPM × default capacity_ratio 0.7


def test_rate_stages_skip_warmup_and_concurrency_stages_fill_every_slot(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)
    calls = []

    def fake(arguments, task_id, timeout):
        calls.append(arguments)
        return native(total=arguments.number[0], succeeded=arguments.number[0])

    with patch('relay_lab.search._execute', fake):
        run = lab.start(config(goal='both', max_requests=400, max_steps=1, refine_steps=0))
        report = wait_done(lab, run.run_id)
    rate_calls = [call for call in calls if call.rate[0] != -1]
    slot_calls = [call for call in calls if call.rate[0] == -1]
    assert rate_calls and slot_calls
    assert all(call.warmup_num == 0 for call in rate_calls)
    assert all(call.warmup_num == call.parallel[0] for call in slot_calls)
    assert report.reserved_requests == sum(call.number[0] + call.warmup_num for call in calls)


def test_unknown_metrics_do_not_become_a_boundary(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)
    with patch('relay_lab.search._execute', lambda *args, **kwargs: native(ttft=None)):
        run = lab.start(config(goal='rpm'))
        report = wait_done(lab, run.run_id)
    assert report.findings[0].status == 'inconclusive'
    assert report.findings[0].value is None


def test_zero_success_stage_reports_the_upstream_error(tmp_path: Path) -> None:
    """A 0% stage used to say "check the native log" and nothing more."""
    lab = CapacityLab(tmp_path)
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=401, text='{"error": "invalid_api_key"}')

    with (
        patch('relay_lab.search._execute', lambda *args, **kwargs: native(succeeded=0)),
        patch('relay_lab.diagnose.httpx.post', fake_post),
    ):
        run = lab.start(config(goal='rpm', max_steps=1, refine_steps=0))
        report = wait_done(lab, run.run_id)
    assert report.stages[0].status == 'failed'
    assert '单请求诊断：HTTP 401' in report.stages[0].reason
    assert '认证失败' in report.stages[0].reason
    assert len(calls) == 1, 'one diagnostic request per run, not per stage'


def test_no_second_run_and_stop_prevents_more_stages(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def fake(arguments, task_id, timeout):
        entered.set()
        release.wait(3)
        return native()

    with patch('relay_lab.search._execute', fake):
        run = lab.start(config())
        assert entered.wait(3)
        with pytest.raises(RuntimeError):
            lab.start(config())
        lab.stop(run.run_id)
        release.set()
        report = wait_done(lab, run.run_id)
    assert report.status == 'stopped'
    assert len(report.stages) == 1


def test_restart_marks_unfinished_runs_interrupted(tmp_path: Path) -> None:
    from relay_lab import store
    from relay_lab.config import LabRun

    settings = LabSettings(url='http://127.0.0.1:1', model='mock')
    run = LabRun(
        run_id='relay-recovered',
        created_at='2026-11-06T00:00:00Z',
        settings=settings,
        fingerprint=fingerprint(settings),
    )
    store.save(tmp_path, run)
    assert CapacityLab(tmp_path).history()[0].status == 'interrupted'


# ---------- dataset presets ----------


def test_dataset_preset_resolves_to_its_input_length() -> None:
    assert LabSettings(url='http://127.0.0.1:1', model='m', dataset_preset='long-short').input_tokens == 4096
    assert LabSettings(url='http://127.0.0.1:1', model='m', dataset_preset='short-short').input_tokens == 64
    # "default" means the small built-in prompt, which is spelled as None.
    assert LabSettings(url='http://127.0.0.1:1', model='m', dataset_preset='default').input_tokens is None


def test_unknown_dataset_preset_is_rejected_not_ignored() -> None:
    """A typo must not silently fall back to the default workload."""
    with pytest.raises(ValidationError):
        LabSettings(url='http://127.0.0.1:1', model='m', dataset_preset='short-shrot')


def test_preset_conflicting_with_an_explicit_length_is_rejected() -> None:
    """Accepting one silently would make the report's stated workload untrue."""
    with pytest.raises(ValidationError):
        LabSettings(url='http://127.0.0.1:1', model='m', dataset_preset='short-short', input_tokens=999)
    # Agreeing with the preset is not a conflict.
    assert LabSettings(
        url='http://127.0.0.1:1', model='m', dataset_preset='short-short', input_tokens=64
    ).input_tokens == 64


@pytest.mark.parametrize('target', [8, 64, 512, 4096, 16000])
def test_generated_prompts_are_always_single_line(target: int) -> None:
    """The dataset plugin reads the prompt file line by line.

    An embedded newline would split one prompt into several shorter ones with no error,
    silently changing the workload and every number derived from it.
    """
    prompts = engine.prompts('rpm', 3, 42, target)
    assert len(prompts) == 3
    assert all('\n' not in prompt and '\r' not in prompt for prompt in prompts)


@pytest.mark.parametrize('phase', ['rpm', 'tpm', 'speed'])
def test_prompt_length_tracks_the_requested_size(phase: str) -> None:
    """Roughly, and always ordered: the point is a repeatable size, not an exact count."""
    small = engine.prompts(phase, 1, 42, 64)[0]
    large = engine.prompts(phase, 1, 42, 4096)[0]
    assert len(small) < len(large)
    assert engine.prompt_tokens(phase, 42, 4096) > 20 * engine.prompt_tokens(phase, 42, 64)


def test_default_prompt_is_unchanged_by_the_new_option() -> None:
    """Backward compatibility: an unset length must reproduce the original prompt."""
    assert engine.prompts('rpm', 1, 42) == engine.prompts('rpm', 1, 42, None)


def test_instruction_survives_the_long_context() -> None:
    """Filler goes after the instruction, so the model still gets a real task."""
    prompt = engine.prompts('rpm', 1, 42, 4096)[0]
    assert prompt.startswith('Case 42-000000. ')
    assert engine.INSTRUCTIONS['rpm'] in prompt
    assert engine.TAILS['rpm'] in prompt


def test_long_input_raises_the_per_request_token_reservation() -> None:
    """A 4096-token input costs ~60x the default; budgeting on the small prompt would
    let a run overshoot its declared token ceiling by orders of magnitude.

    Compared against the *input* portion only: the reservation also carries a fixed
    cushion for headers and system overhead, which dilutes the ratio on small prompts.
    """
    def input_portion(input_tokens: int | None) -> int:
        return engine.prompt_tokens('rpm', 42, input_tokens) * engine.CHARS_PER_TOKEN

    small, large = input_portion(None), input_portion(4096)
    assert large > 50 * small
    # And the full reservation must still grow substantially, not just the input slice.
    assert large + 2080 > 5 * (small + 2080)


def test_budget_exhausts_sooner_with_a_long_input(tmp_path: Path) -> None:
    """The reservation is what actually stops a long-input run from overspending."""
    lab = CapacityLab(tmp_path)
    calls: list[int] = []

    def fake(arguments, task_id, timeout):
        calls.append(len(arguments.dataset_path))
        return native(total=arguments.number[0], succeeded=arguments.number[0])

    def stage_count(input_tokens: int | None) -> int:
        calls.clear()
        with patch('relay_lab.search._execute', fake):
            run = lab.start(config(goal='rpm', input_tokens=input_tokens, token_reserve=200000, max_requests=5000))
            report = wait_done(lab, run.run_id)
        return len(report.stages)

    assert stage_count(4096) < stage_count(None)


def test_preset_is_recorded_in_the_run_warnings(tmp_path: Path) -> None:
    """A number is only comparable if the report says which workload produced it."""
    lab = CapacityLab(tmp_path)
    with patch('relay_lab.search._execute', lambda *a, **k: native()):
        run = lab.start(config(goal='rpm', dataset_preset='long-short'))
        report = wait_done(lab, run.run_id)
    assert any('长入短出' in warning and '不可直接比较' in warning for warning in report.warnings), report.warnings


def test_api_serves_the_preset_catalog(tmp_path: Path) -> None:
    """The console reads this instead of hard-coding names that could drift."""
    client = TestClient(create_app(tmp_path, web_dir=tmp_path / 'missing', schedule_loop=False))
    login(client)
    payload = client.get('/api/presets').json()
    keys = [item['key'] for item in payload['datasets']]
    assert 'short-short' in keys and 'long-short' in keys
    assert all('name' in item and 'note' in item for item in payload['datasets'])


# ---------- http surface ----------


def test_api_validation_and_same_origin(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, web_dir=tmp_path / 'missing', schedule_loop=False))
    assert client.get('/api/runs').status_code == 401
    assert client.get('/api/health').json()['ok'] is True
    login(client)
    assert client.get('/api/runs').json() == {'runs': []}
    invalid = client.post('/api/runs', json={'url': 'http://localhost', 'model': 'x'})
    assert invalid.status_code == 400
    assert 'error' in invalid.json()
    assert client.post('/api/runs', json={}, headers={'Origin': 'https://untrusted.test'}).status_code == 403
    assert client.get('/api/runs/nonexistent').status_code == 404
    assert client.post('/api/runs/nonexistent/stop', json={}).status_code == 404
    assert client.get('/api/docs').status_code == 200


def test_console_is_optional_and_served_when_present(tmp_path: Path) -> None:
    standalone = TestClient(create_app(tmp_path, web_dir=tmp_path / 'missing', schedule_loop=False))
    assert standalone.get('/').status_code == 503
    assert standalone.get('/').json()['console'] == 'not built'

    web = tmp_path / 'web'
    web.mkdir()
    (web / 'index.html').write_text('<h1>console</h1>', encoding='utf-8')
    served = TestClient(create_app(tmp_path, web_dir=web, schedule_loop=False))
    assert served.get('/').status_code == 200
    assert 'console' in served.get('/').text


def test_token_budget_grows_with_input_length() -> None:
    """A long-context preset must not look affordable just because request count is low."""
    short = LabSettings(url='http://127.0.0.1:1', model='m', dataset_preset='short-short')
    long = LabSettings(url='http://127.0.0.1:1', model='m', dataset_preset='long-short')
    assert required_tokens(long) > 4 * required_tokens(short)
    # The default reserve must not silently look sufficient for a long-input run.
    assert required_tokens(long) > long.token_reserve > required_tokens(short)


def test_latency_floor_stops_the_search_instead_of_halving(tmp_path: Path) -> None:
    """P50 above the SLA means the site is slow even unloaded; halving cannot fix it."""
    lab = CapacityLab(tmp_path)
    calls = []

    def fake(arguments, task_id, timeout):
        calls.append(arguments)
        # 100% success, but P50 = 3500 ms (native() halves ttft for the 50% row)
        # while the SLA is 3000 ms: every rate would fail for the same reason.
        return native(ttft=7000)

    with patch('relay_lab.search._execute', fake):
        run = lab.start(config(goal='rpm', start_rpm=60, max_rpm=480, max_requests=400, max_steps=4, refine_steps=2))
        report = wait_done(lab, run.run_id)
    assert len(report.stages) == 1, 'a latency floor must not trigger further halving'
    assert report.findings[0].status == 'latency_floor'
    assert 'P50' in report.findings[0].message


def test_failure_reasons_are_counted_from_the_request_log(tmp_path: Path) -> None:
    import sqlite3 as sql

    from relay_lab.search import _failure_breakdown

    database = tmp_path / 'benchmark_data.db'
    connection = sql.connect(database)
    connection.execute('CREATE TABLE result(success INTEGER, error TEXT, status_code INTEGER)')
    connection.executemany(
        'INSERT INTO result VALUES (?, ?, ?)',
        [
            (0, 'TimeoutError: total timeout', None),
            (0, 'TimeoutError: read timeout', None),
            (0, '{"error": "rate limit"}', 429),
            (1, None, None),
        ],
    )
    connection.commit()
    connection.close()
    assert _failure_breakdown(database) == {'超时': 2, 'HTTP 429': 1}
    assert _failure_breakdown(tmp_path / 'missing.db') == {}


def test_failed_request_keeps_its_reason_in_the_log(tmp_path: Path) -> None:
    """Before this, failures were stored with no reason, so 429s looked like timeouts."""
    import sqlite3 as sql

    from relay_lab.vendor.perf.utils import db_util
    from relay_lab.vendor.perf.utils.benchmark_util import BenchmarkData

    database = tmp_path / 'native.db'
    connection = sql.connect(database)
    db_util.create_result_table(connection.cursor())
    db_util.insert_benchmark_data(
        connection.cursor(),
        BenchmarkData(
            request='{}',
            start_time=1.0,
            completed_time=2.0,
            success=False,
            error='TimeoutError: read timeout',
        ),
    )
    connection.commit()
    error, status = connection.execute('SELECT error, status_code FROM result').fetchone()
    assert error.startswith('TimeoutError')
    assert status is None


def test_websocket_pushes_run_changes(tmp_path: Path) -> None:
    """Every save reaches the console immediately; polling is no longer the transport."""
    app = create_app(tmp_path, schedule_loop=False)
    with TestClient(app) as client:
        login(client)
        with client.websocket_connect('/api/ws') as socket:
            snapshot = socket.receive_json()
            assert snapshot['type'] == 'snapshot'
            assert snapshot['runs'] == []

            settings = SpeedSettings(url='http://127.0.0.1:1', model='m')
            store.save(tmp_path, LabRun(run_id='relay-ws-test', created_at='2026-11-09T00:00:00Z',
                                        kind='speed', settings=settings, fingerprint='f'))
            pushed = socket.receive_json()

    assert pushed['type'] == 'run'
    assert pushed['run']['run_id'] == 'relay-ws-test'


def test_a_broken_listener_never_blocks_persistence(tmp_path: Path) -> None:
    """The push hub is UI plumbing; a crash inside it must not lose an experiment."""

    def explode(run: LabRun) -> None:
        raise RuntimeError('listener bug')

    store.subscribe(explode)
    try:
        settings = SpeedSettings(url='http://127.0.0.1:1', model='m')
        run = LabRun(run_id='relay-safe', created_at='2026-11-09T00:00:00Z', kind='speed',
                     settings=settings, fingerprint='f')
        store.save(tmp_path, run)
    finally:
        store.unsubscribe(explode)
    assert (tmp_path / 'relay-safe' / 'experiment.json').is_file()


def test_prompts_differ_beyond_the_case_number() -> None:
    """Two requests must not be the same text with a different counter."""
    batch = engine.prompts('rpm', 6, 42)
    assert len(set(batch)) == 6
    bodies = {prompt.split('. ', 1)[1] for prompt in batch}
    assert len(bodies) > 1, 'the closing sentence should vary between requests'


def test_reuse_mode_sends_the_same_prompt_every_time() -> None:
    batch = engine.prompts('rpm', 5, 42, reuse=True)
    assert len(set(batch)) == 1
    # Reuse must also survive the long-input path, or the cache test means nothing.
    long_batch = engine.prompts('rpm', 3, 42, input_tokens=512, reuse=True)
    assert len(set(long_batch)) == 1


def test_capacity_run_states_the_prompt_reuse_caliber(tmp_path: Path) -> None:
    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        return native(total=arguments.number[0], succeeded=arguments.number[0])

    with patch('relay_lab.search._execute', fake):
        cold = lab.start(config(goal='rpm', max_steps=1, refine_steps=0))
        wait_done(lab, cold.run_id)
        reuse = lab.start(config(goal='rpm', max_steps=1, refine_steps=0, prompt_reuse=True,
                                 label='reuse'))
        wait_done(lab, reuse.run_id)
    assert any('冷启动' in warning for warning in cold.warnings)
    assert any('缓存命中' in warning for warning in reuse.warnings)


def test_stage_prompt_file_is_identical_under_reuse(tmp_path: Path) -> None:
    """The file handed to the engine is the real workload; check it, not just the helper."""
    lab = CapacityLab(tmp_path)

    def fake(arguments, task_id, timeout):
        return native(total=arguments.number[0], succeeded=arguments.number[0])

    with patch('relay_lab.search._execute', fake):
        run = lab.start(config(goal='rpm', max_steps=1, refine_steps=0, prompt_reuse=True))
        report = wait_done(lab, run.run_id)
    prompts = (tmp_path / report.run_id / 'stage-001' / 'prompts.txt').read_text(encoding='utf-8').splitlines()
    assert len(set(prompts)) == 1

def test_read_progress_counts_completed_requests(tmp_path: Path) -> None:
    import sqlite3 as sql

    from relay_lab.search import _read_progress

    assert _read_progress(tmp_path / 'missing.db') == (0, 0)
    database = tmp_path / 'native.db'
    connection = sql.connect(database)
    connection.execute('CREATE TABLE result(success INTEGER)')
    connection.executemany('INSERT INTO result VALUES (?)', [(1,), (1,), (0,)])
    connection.commit()
    connection.close()
    assert _read_progress(database) == (3, 2)


def test_stage_publishes_progress_while_the_engine_runs(tmp_path: Path, monkeypatch) -> None:
    """A long stage must not look frozen: counts arrive before it finishes."""
    lab = CapacityLab(tmp_path)
    seen: list[int] = []

    def listener(run: LabRun) -> None:
        for stage in run.stages:
            if stage.status == 'running' and stage.requests:
                seen.append(stage.requests)

    def slow(arguments, task_id, timeout):
        time.sleep(0.4)
        return native(total=arguments.number[0], succeeded=arguments.number[0])

    store.subscribe(listener)
    monkeypatch.setattr('relay_lab.search.PROGRESS_INTERVAL', 0.05)
    monkeypatch.setattr('relay_lab.search._read_progress', lambda database: (7, 7))
    try:
        with patch('relay_lab.search._execute', slow):
            run = lab.start(config(goal='rpm', max_steps=1, refine_steps=0))
            wait_done(lab, run.run_id)
    finally:
        store.unsubscribe(listener)
    assert seen, 'mid-stage progress was never published'
    assert max(seen) == 7
