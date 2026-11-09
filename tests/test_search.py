"""Behaviour tests for the lab's own controller; no network and no credentials."""

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from relay_lab.api import create_app
from relay_lab.config import LabSettings, LabStage, StartRequest
from relay_lab.engine import summarize
from relay_lab.search import CapacityLab, fingerprint
from relay_lab.vendor.perf.utils.perf_models import BenchmarkSummary, PercentileResult, PercentileRow


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
            avg_input_tokens=24,
            avg_output_tokens=16,
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


def stage(phase='rpm') -> LabStage:
    return LabStage(index=1, phase=phase, pressure=60, repeat=1, native_task_id='local-test')


def wait_done(lab: CapacityLab, run_id: str):
    assert lab.jobs[run_id].done.wait(10)
    return next(item for item in lab.history() if item.run_id == run_id)


# ---------- summarising ----------


def test_minute_units_and_distinct_total_tpm() -> None:
    observed = summarize(stage('tpm'), native(), config())
    assert observed.rpm == 60
    assert observed.input_tpm == 1440
    assert observed.output_tpm == 960
    assert observed.total_tpm == 2400
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
    assert report.findings[0].value == 60


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


# ---------- http surface ----------


def test_api_validation_and_same_origin(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, web_dir=tmp_path / 'missing', schedule_loop=False))
    assert client.get('/api/runs').json() == {'runs': []}
    assert client.get('/api/health').json()['ok'] is True
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
