"""Run control for every test kind.

Capacity: doubling ascent, halving descent when the first pressure already
fails, bisection between pass and fail, under a three-way budget.

Speed: a fixed number of requests at a fixed concurrency, reported as latency.

Parallel: one stage per requested concurrency level, reported as a ladder.

Quality: a built-in item set graded locally, independent of the perf engine.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from . import contract, diagnose, engine, netutil, quality, runner, store
from .config import (
    DATASET_PRESETS,
    AIAnalysis,
    ContractItem,
    ContractRequest,
    ContractSettings,
    LabFinding,
    LabRun,
    LabSettings,
    LabStage,
    LoadRequest,
    LoadSettings,
    LoadSummary,
    ParallelRequest,
    ParallelSettings,
    QualityItem,
    QualityRequest,
    QualitySettings,
    SpeedRequest,
    SpeedSettings,
    StartRequest,
)
from .model_profiles import resolve_profile

Phase = Literal['rpm', 'tpm']
AnySettings = LabSettings | SpeedSettings | ParallelSettings | QualitySettings | ContractSettings

DONE_MESSAGES = store.DONE_MESSAGES
STALE_PREFIXES = store.STALE_PREFIXES
# In-flight progress lines that should not survive as the final card description.
_PROGRESS_PREFIXES = STALE_PREFIXES + ('第 ', '正在执行', '正在批改', '正在运行', '已完成 ')


PROGRESS_INTERVAL = 0.6
"""Seconds between mid-stage progress writes, so a 60 s window is not a black box."""


class BudgetExhausted(Exception):
    """No further requests may be admitted under the selected experiment limits."""


def fingerprint(settings: AnySettings) -> str:
    """Identify matching workloads without including the target or any credential."""
    data = settings.model_dump(exclude={'url', 'label'})
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def _id() -> str:
    return f'relay-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}'


@dataclass
class _Job:
    run: LabRun
    wall_limit: float = 600
    stop: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    active_task: str | None = None
    started: float = field(default_factory=time.monotonic)
    diagnosis: str | None = None
    """One diagnostic request per run, reused by every later all-failed stage."""


def _execute(arguments: Any, task_id: str, timeout: float) -> Any:
    """Single seam for the child-process call, so tests can replace it."""
    return runner.run(engine.run_native, arguments, task_id=task_id, timeout=timeout)


def demand_of(phase: Phase, pressure: int, settings: LabSettings) -> int:
    """Requests a stage must be able to send before its window is measurable.

    Rate mode is exact pacing: the window needs pressure/60 arrivals per second.
    Closed loop is bounded by slots, and a long-output request cannot cycle faster
    than roughly one per 10 seconds, so queue depth stays near the real dispatch count
    instead of reserving duration * slots * 2.
    """
    if phase == 'rpm':
        return max(3, math.ceil(pressure / 60 * settings.stage_seconds))
    return max(8, pressure * max(2, math.ceil(settings.stage_seconds / 10)))


def ladder_of(settings: LabSettings, phase: Phase) -> list[int]:
    """The pressures an upward search would visit, in order."""
    ceiling = settings.max_rpm if phase == 'rpm' else settings.max_tpm_parallel
    current = settings.start_rpm if phase == 'rpm' else 1
    rungs = [current]
    while current < ceiling and len(rungs) < settings.max_steps:
        current = min(ceiling, current * 2)
        rungs.append(current)
    return rungs


def required_requests(settings: LabSettings, phases: list[Phase] | None = None) -> int:
    """Budget needed by the configured ladder, plus an allowance for bisect retests."""
    phases = phases or (['rpm', 'tpm'] if settings.goal == 'both' else [settings.goal])
    total = 0
    for phase in phases:
        rungs = ladder_of(settings, phase)
        total += sum(demand_of(phase, rung, settings) for rung in rungs) * settings.repeats
        total += demand_of(phase, rungs[-1] // 2 or 1, settings) * settings.refine_steps
    return total


def required_tokens(settings: LabSettings, phases: list[Phase] | None = None) -> int:
    """Token reservation the same ladder will consume at the configured input length.

    A long-context dataset costs an order of magnitude more tokens per request than the
    built-in short prompt, so the request budget alone says nothing about whether the run
    will finish. Counted with exactly the same ladder and reservation formula the planner
    uses, so the warning and the actual abort point cannot disagree.
    """
    phases = phases or (['rpm', 'tpm'] if settings.goal == 'both' else [settings.goal])
    total = 0
    for phase in phases:
        output_limit = settings.short_output_tokens if phase == 'rpm' else settings.long_output_tokens
        per_request = (
            engine.prompt_tokens(phase, settings.seed, settings.input_tokens) * engine.CHARS_PER_TOKEN
            + 2048
            + output_limit
        )
        rungs = ladder_of(settings, phase)
        count = sum(demand_of(phase, rung, settings) for rung in rungs) * settings.repeats
        count += demand_of(phase, rungs[-1] // 2 or 1, settings) * settings.refine_steps
        total += count * per_request
    return total


def _read_progress(database: Path) -> tuple[int, int]:
    """Completed and successful requests so far, straight from the engine's log.

    Only used to draw a live row; a locked or not-yet-created database simply means
    "nothing to report yet", never an error for the stage itself.
    """
    if not database.exists():
        return 0, 0
    try:
        connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=0.3)
        try:
            total, ok = connection.execute('select count(*), coalesce(sum(success), 0) from result').fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return 0, 0
    return int(total or 0), int(ok or 0)


def _token_source(succeeded: int, reported: int, estimated: int, missing: int) -> Literal['reported', 'estimated', 'missing', 'unknown']:
    if missing:
        return 'missing'
    if estimated:
        return 'estimated'
    return 'reported' if succeeded and reported == succeeded else 'unknown'


def _read_live_sample(database: Path) -> dict[str, Any]:
    """Latency and token totals for the in-flight window. Missing columns yield nothing."""
    if not database.exists():
        return {}
    try:
        connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=0.3)
        try:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(result)')}
            wanted = {'latency', 'first_chunk_latency', 'prompt_tokens', 'completion_tokens', 'is_stream', 'success'}
            if not wanted <= columns:
                return {}
            source = 'token_source' if 'token_source' in columns else 'NULL'
            row = connection.execute(
                'select avg(case when success = 1 and latency > 0 then latency end), '
                'avg(case when success = 1 and is_stream = 1 and first_chunk_latency > 0 then first_chunk_latency end), '
                'coalesce(sum(case when success = 1 then prompt_tokens end), 0), '
                'coalesce(sum(case when success = 1 then completion_tokens end), 0), '
                'count(case when success = 1 then 1 end), '
                f"count(case when success = 1 and {source} = 'reported' then 1 end), "
                f"count(case when success = 1 and {source} = 'estimated' then 1 end), "
                f"count(case when success = 1 and {source} = 'missing' then 1 end) from result"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    latency, ttft, prompt, completion, ok, reported, estimated, missing = row
    sample: dict[str, Any] = {'token_source': _token_source(ok, reported, estimated, missing)}
    if latency:
        sample['latency_ms'] = float(latency) * 1000
    if ttft:
        sample['ttft_ms'] = float(ttft) * 1000
    sample['prompt_tokens'] = float(prompt or 0)
    sample['completion_tokens'] = float(completion or 0)
    return sample


@dataclass(frozen=True)
class _WindowStats:
    completed: int
    succeeded: int
    span: float
    dispatch_span: float
    prompt_tokens: int
    completion_tokens: int
    token_source: Literal['reported', 'estimated', 'missing', 'unknown']


def _window_stats(database: Path, window_seconds: int, stage: LabStage) -> _WindowStats | None:
    """Use a single log snapshot and reject one that disagrees with the engine totals."""
    if not database.exists():
        return None
    try:
        connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=0.3)
        try:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(result)')}
            source = 'token_source' if 'token_source' in columns else 'NULL'
            row = connection.execute(
                f'''with bounds as (select min(start_time) as started from result),
                        samples as (
                            select *, completed_time - started <= ? as in_window
                            from result cross join bounds
                        )
                   select count(*), count(case when success = 1 then 1 end),
                          count(case when typeof(start_time) in ('real', 'integer')
                                      and typeof(completed_time) in ('real', 'integer')
                                      and completed_time >= start_time and success in (0, 1) then 1 end),
                          max(completed_time) - min(start_time), max(start_time) - min(start_time),
                          count(case when in_window then 1 end),
                          count(case when in_window and success = 1 then 1 end),
                          sum(case when in_window and success = 1 then prompt_tokens end),
                          sum(case when in_window and success = 1 then completion_tokens end),
                          count(case when in_window and success = 1 and {source} = 'reported' then 1 end),
                          count(case when in_window and success = 1 and {source} = 'estimated' then 1 end),
                          count(case when in_window and success = 1 and {source} = 'missing' then 1 end)
                   from samples''',
                (window_seconds,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    total, success_total, valid, span, dispatch_span, completed, succeeded, prompt, completion, reported, estimated, missing = row
    if total != stage.requests or success_total != stage.succeeded or valid != total:
        return None
    if span is None or not math.isfinite(span):
        return None
    return _WindowStats(completed, succeeded, span, dispatch_span, int(prompt or 0), int(completion or 0),
                        _token_source(succeeded, reported, estimated, missing))


def _read_live_circuit(database: Path) -> tuple[int, int, int, float | None]:
    """读取运行中已落库请求的熔断指标：(总数, 成功数, 最大连续失败数, 平均延迟ms)。"""
    if not database.exists():
        return 0, 0, 0, None
    try:
        connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=0.3)
        try:
            rows = connection.execute(
                'select success, latency from result order by completed_time, start_time'
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return 0, 0, 0, None
    total = len(rows)
    if not total:
        return 0, 0, 0, None
    ok = 0
    longest = 0
    streak = 0
    lat_sum = 0.0
    lat_count = 0
    for success, latency in rows:
        if success:
            ok += 1
            streak = 0
            if isinstance(latency, (int, float)) and math.isfinite(latency) and latency > 0:
                lat_sum += float(latency)
                lat_count += 1
        else:
            streak += 1
            if streak > longest:
                longest = streak
    avg_latency_ms = (lat_sum / lat_count * 1000) if lat_count else None
    return total, ok, longest, avg_latency_ms


def _load_circuit_reason(database: Path, settings: LoadSettings) -> str | None:
    """运行中实时检查熔断条件；命中则返回熔断原因以立即中止子进程。"""
    total, ok, consecutive, avg_latency_ms = _read_live_circuit(database)
    if not total:
        return None
    if settings.max_consecutive_failures > 0 and consecutive >= settings.max_consecutive_failures:
        return f'熔断：连续失败 {consecutive} 次，达到阈值 {settings.max_consecutive_failures}'
    # 至少累积 20 个样本再按成功率/平均延迟熔断，避免前几个偶发失败误杀整场。
    min_samples = max(20, min(settings.max_consecutive_failures or 20, 50))
    if total >= min_samples and settings.circuit_success_percent > 0:
        rate = 100.0 * ok / total
        if rate < settings.circuit_success_percent:
            return f'熔断：成功率 {rate:.1f}% 低于 {settings.circuit_success_percent:g}%，视为过载'
    if (
        total >= min_samples
        and settings.avg_latency_ms > 0
        and avg_latency_ms is not None
        and avg_latency_ms > settings.avg_latency_ms
    ):
        return f'熔断：平均响应 {avg_latency_ms:.0f} ms 超过 {settings.avg_latency_ms:g} ms，终止本场景'
    return None


def _latency_percentiles(database: Path, window_seconds: int | None = None) -> dict[str, float | None]:
    """Read only timing columns, not response bodies or per-chunk JSON."""
    if not database.is_file():
        return {}
    try:
        con = sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=0.3)
        try:
            cutoff = ' and completed_time - (select min(start_time) from result) <= ?' if window_seconds is not None else ''
            rows = con.execute(
                'select latency, case when is_stream then first_chunk_latency end, '
                'case when is_stream and completion_tokens > 1 then time_per_output_token end '
                'from result where success = 1' + cutoff,
                (window_seconds,) if window_seconds is not None else (),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    metrics = {}
    for index, (name, ranks) in enumerate((('latency', (50, 90, 95, 99)), ('ttft', (50, 95, 99)), ('tpot', (50, 95, 99)))):
        values = sorted(r[index] for r in rows if isinstance(r[index], (float, int)) and math.isfinite(r[index]) and r[index] > 0)
        for rank in ranks:
            metrics[f'{name}_p{rank}_ms'] = (
                round(1000 * values[math.ceil(len(values) * rank / 100) - 1], 3) if values else None
            )
    return metrics


def _observe_from_db(stage: LabStage, database: Path, elapsed: float) -> LabStage:
    """当子进程提前中止时，从已落库的 SQLite 结果重建统计与分位数。"""

    observed = stage.model_copy(deep=True)
    seconds = max(0.001, elapsed)
    observed.seconds = round(seconds, 3)
    if not database.exists():
        return observed
    try:
        connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=0.3)
        try:
            row = connection.execute(
                'select count(*), coalesce(sum(success), 0), '
                'avg(case when success = 1 and latency > 0 then latency end), '
                'coalesce(sum(case when success = 1 then prompt_tokens end), 0), '
                'coalesce(sum(case when success = 1 then completion_tokens end), 0), '
                'min(start_time), max(completed_time) '
                'from result'
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return observed
    if not row:
        return observed
    total, ok, avg_lat, prompt_sum, comp_sum, first_start, last_done = row
    total = int(total or 0)
    ok = int(ok or 0)
    if isinstance(first_start, (int, float)) and isinstance(last_done, (int, float)) and last_done > first_start:
        seconds = float(last_done - first_start)
        observed.seconds = round(seconds, 3)
    observed.requests = total
    observed.succeeded = ok
    observed.success_percent = 100 * ok / total if total else 0.0
    observed.rpm = round(ok / seconds * 60, 1) if ok else 0.0
    observed.peak_concurrency = float(stage.pressure)
    if avg_lat:
        avg_sec = float(avg_lat)
        observed.latency_avg_ms = round(avg_sec * 1000, 1)
        if ok:
            observed.mean_concurrency = round((ok / seconds) * avg_sec, 2)
    if ok and prompt_sum:
        observed.input_tpm = float(prompt_sum) / seconds * 60
        observed.mean_input_tokens = float(prompt_sum) / ok
    if ok and comp_sum:
        observed.output_tpm = float(comp_sum) / seconds * 60
        observed.mean_output_tokens = float(comp_sum) / ok
    if observed.input_tpm is not None and observed.output_tpm is not None:
        observed.total_tpm = observed.input_tpm + observed.output_tpm
    for attr, value in _latency_percentiles(database).items():
        setattr(observed, attr, value)
    return observed


def _format_failure_detail(breakdown: dict[str, int]) -> str:
    return '，'.join(
        f'{key} ×{count}' for key, count in sorted(breakdown.items(), key=lambda item: (-item[1], item[0]))
    )


def _failure_key(error: str | None, status_code: int | None) -> str:
    """Collapse one failure into a short, comparable cause."""
    if status_code:
        return f'HTTP {status_code}'
    text = ' '.join((error or '').split())
    if not text:
        return '未记录原因'
    low = text.lower()
    if 'timeout' in low:
        return '超时'
    if 'cannot connect' in low or 'connector' in low:
        return '连接失败'
    if 'reset' in low or 'disconnect' in low:
        return '连接中断'
    return text[:48]


def _failure_breakdown(database: Path) -> dict[str, int]:
    """Count why requests failed, straight from the engine's per-request log.

    The summary only knows *how many* failed. Without this, a rate-limited stage and a
    stage whose requests timed out look identical, and the report cannot tell the user
    which one they are looking at.
    """
    if not database.exists():
        return {}
    try:
        connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
        try:
            rows = connection.execute('select error, status_code from result where success = 0').fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return {}
    counts: dict[str, int] = {}
    for error, status_code in rows:
        key = _failure_key(error, status_code)
        counts[key] = counts.get(key, 0) + 1
    return counts


def latency_floor(stage: LabStage, settings: LabSettings) -> bool:
    """True when the site is slower than the SLA before it is even loaded.

    Success rate is fine, yet the *median* first token already exceeds the P99 limit.
    Halving the rate cannot fix that — it is a latency floor, not a capacity ceiling —
    so the search must stop instead of burning stages on a ladder that cannot pass.
    """
    return (
        stage.status == 'failed'
        and stage.success_percent >= settings.success_percent
        and stage.ttft_p50_ms is not None
        and stage.ttft_p50_ms > settings.ttft_p99_ms
    )


def _max_consecutive_failures(database: Path) -> int:
    """Longest run of failed rows in completion order; 0 when the log is empty."""
    if not database.exists():
        return 0
    try:
        connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
        try:
            rows = connection.execute(
                'select success from result order by completed_time, start_time'
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return 0
    longest = 0
    streak = 0
    for (success,) in rows:
        if success:
            streak = 0
            continue
        streak += 1
        if streak > longest:
            longest = streak
    return longest


def _load_verdict(
    observed: LabStage,
    settings: LoadSettings,
    *,
    consecutive_failures: int = 0,
) -> LabStage:
    """Apply circuit-breaker then SLA: overload ends the scene; SLA gates the peak."""
    if not observed.requests:
        observed.status = 'unknown'
        observed.reason = '本窗口没有完成的请求'
        return observed

    if settings.circuit_success_percent > 0 and observed.success_percent < settings.circuit_success_percent:
        observed.status = 'failed'
        observed.reason = (
            f'熔断：成功率 {observed.success_percent:.1f}% 低于 '
            f'{settings.circuit_success_percent:g}%，视为过载'
        )
        return observed
    if (
        settings.max_consecutive_failures > 0
        and consecutive_failures >= settings.max_consecutive_failures
    ):
        observed.status = 'failed'
        observed.reason = (
            f'熔断：连续失败 {consecutive_failures} 次，达到阈值 '
            f'{settings.max_consecutive_failures}'
        )
        return observed
    if (
        settings.avg_latency_ms > 0
        and observed.latency_avg_ms is not None
        and observed.latency_avg_ms > settings.avg_latency_ms
    ):
        observed.status = 'failed'
        observed.reason = (
            f'熔断：平均响应 {observed.latency_avg_ms:.0f} ms 超过 '
            f'{settings.avg_latency_ms:g} ms，终止本场景'
        )
        return observed

    window_rate = observed.window_success_percent
    if observed.window_requests is None:
        observed.status = 'unknown'
        observed.reason = '无法核实窗口数据：逐请求日志缺失或不完整，不计入峰值'
        return observed
    if window_rate is None:
        observed.status = 'unknown'
        observed.reason = '窗口内没有完成的请求，不计入峰值'
        return observed
    if window_rate < settings.success_percent:
        observed.status = 'failed'
        observed.reason = (
            f'窗口成功率 {window_rate:.1f}% 低于 SLA 下限 '
            f'{settings.success_percent:g}%；该窗口吞吐不计入峰值'
        )
        return observed
    if not observed.window_succeeded:
        observed.status = 'unknown'
        observed.reason = '窗口内没有成功请求，不计入峰值'
        return observed
    if settings.stream:
        if observed.ttft_p99_ms is None:
            observed.status = 'unknown'
            observed.reason = '缺少流式首字节，无法确认 SSE 首包'
            return observed
        if observed.ttft_p99_ms > settings.ttft_p99_ms:
            observed.status = 'failed'
            observed.reason = (
                f'TTFT P99 {observed.ttft_p99_ms:.0f} ms 超过 SLA 上限 '
                f'{settings.ttft_p99_ms:g} ms'
            )
            return observed
        if observed.token_source in ('estimated', 'missing'):
            observed.status = 'unknown'
            observed.reason = '窗口内 token 用量含估算或缺失，无法验证 TPOT SLA，不计入峰值'
            return observed
        if observed.tpot_p99_ms is None or observed.tpot_p99_ms <= 0:
            observed.status = 'unknown'
            observed.reason = '没有有效 TPOT 样本（例如仅生成 1 个 token），未验证 TPOT SLA，不计入峰值'
            return observed
        if observed.tpot_p99_ms > settings.tpot_p99_ms:
            observed.status = 'failed'
            observed.reason = (
                f'TPOT P99 {observed.tpot_p99_ms:.1f} ms/token 超过 SLA 上限 '
                f'{settings.tpot_p99_ms:g} ms'
            )
            return observed

    observed.status = 'passed'
    observed.reason = f'并发 {observed.pressure}，窗口成功率 {window_rate:.1f}%'
    return observed


def _apply_load_window(stage: LabStage, window: _WindowStats | None, seconds: float) -> None:
    if window is not None:
        stage.window_requests = window.completed
        stage.window_succeeded = window.succeeded
        if window.completed:
            stage.window_success_percent = 100 * window.succeeded / window.completed
        stage.token_source = window.token_source
        if seconds <= 0:
            return
        stage.window_rate_seconds = seconds
        scale = 60 / seconds
        stage.rpm = round(window.succeeded * scale, 1)
        if window.token_source == 'missing':
            stage.input_tpm = stage.output_tpm = stage.total_tpm = None
        else:
            stage.input_tpm = window.prompt_tokens * scale
            stage.output_tpm = window.completion_tokens * scale
            stage.total_tpm = stage.input_tpm + stage.output_tpm


def _partial_load_window(stage: LabStage, database: Path, elapsed: float, window_seconds: int, reason: str, *, failed: bool = False) -> LabStage:
    observed = _observe_from_db(stage, database, elapsed)
    window = _window_stats(database, window_seconds, observed)
    if window is not None:
        observed.seconds = window.span
        _apply_load_window(observed, window, min(window.span, window_seconds))
        for attr, value in _latency_percentiles(database, window_seconds).items():
            setattr(observed, attr, value)
    observed.status = 'failed' if failed else 'unknown'
    observed.partial_results = True
    observed.reason = f'{reason}；部分结果仅统计已落库请求，不计入峰值或建议容量'
    return observed


def _finalize_load_window(stage: LabStage, settings: LoadSettings, database: Path, budget: int) -> LabStage:
    window = _window_stats(database, settings.window_seconds, stage)
    _apply_load_window(stage, window, settings.window_seconds)
    if window is not None:
        for attr, value in _latency_percentiles(database, settings.window_seconds).items():
            setattr(stage, attr, value)
    _load_verdict(stage, settings, consecutive_failures=_max_consecutive_failures(database))
    if window is not None:
        if stage.status == 'passed' and stage.requests >= budget and window.dispatch_span < settings.window_seconds:
            stage.status = 'unknown'
            stage.reason = (
                f'请求预算在窗口截止前已用尽（最后一条在第 {window.dispatch_span:.2f} 秒发出），'
                '尾部回包不能证明持续加压，不计入峰值'
            )
        elif stage.status == 'passed' and window.span < settings.window_seconds * 0.9:
            stage.status = 'unknown'
            stage.reason = (
                f'未跑满 {settings.window_seconds} 秒窗口（{window.span:.1f} 秒即全部完成），'
                '不计入峰值；可提高请求上限重测'
            )
        late = stage.requests - window.completed
        if late:
            stage.reason += f'；窗口内完成 {window.completed} 个，窗口结束后完成 {late} 个（仅计入全程统计）'
    return stage


class CapacityLab:
    """Own one experiment at a time, whatever its kind."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.RLock()
        self.jobs: dict[str, _Job] = {}

    # ---------- history / lifecycle ----------

    def history(self) -> list[LabRun]:
        """Return live snapshots plus persisted reports, never credential material."""
        with self.lock:
            runs = {key: job.run.model_copy(deep=True) for key, job in self.jobs.items()}
        for run in store.load(self.root):
            runs.setdefault(run.run_id, run)
        return sorted(runs.values(), key=lambda run: run.created_at, reverse=True)[:50]

    def get_run(self, run_id: str) -> LabRun | None:
        with self.lock:
            job = self.jobs.get(run_id)
            if job:
                snapshot = job.run.model_copy(deep=True)
                if not job.done.is_set() and snapshot.status not in ('running', 'stopping'):
                    snapshot.status = 'stopping'
                return snapshot
            return store.load_run(self.root, run_id)

    def save_analysis(self, run_id: str, analysis: AIAnalysis) -> None:
        with self.lock:
            run = self.get_run(run_id)
            if run is None or run.status in ('running', 'stopping'):
                raise RuntimeError('报告已不存在或尚未结束')
            run.ai_analysis = analysis
            store.save(self.root, run)
            job = self.jobs.get(run_id)
            if job:
                job.run.ai_analysis = analysis

    def start(self, request: StartRequest) -> LabRun:
        """Start a capacity experiment after validation, without persisting the key."""
        settings = LabSettings.model_validate(request.model_dump(exclude={'authorized'}))
        phases: list[Phase] = ['rpm', 'tpm'] if settings.goal == 'both' else [settings.goal]
        run = LabRun(
            run_id=_id(),
            created_at=datetime.now(timezone.utc).isoformat(),
            kind='capacity',
            settings=settings,
            fingerprint=fingerprint(settings),
            findings=[LabFinding(phase=phase) for phase in phases],
            warnings=[
                '测量的是当前网络、Key、模型和负载下的已观测吞吐，不是整个站点的绝对容量或计费配额。',
                'Token 使用压测引擎统计（接口 usage 或引擎估算）；不是计费证明，不按 SSE 分块计 Token。',
                '使用固定种子的变化请求库；重复请求不代表缓存命中，也不保证消除公共前缀缓存。',
                'RPM 使用带并发上限的速率模式；实际吞吐不足目标也可能受客户端、并发或网络限制。',
                'TPM 档先发送不超过 16 个、且不超过并发数的预热请求；预热不计入统计，但计入请求与 Token 预算。',
                'RPM 档按目标速率逐条发送、不做预热；首个请求的冷启动会计入 TTFT 统计。',
                '每档从压测引擎实际计时开始，包含尾部在途完成时间；不是严格滚动 60 秒配额测量。',
            ],
        )
        if settings.stage_seconds < 60 or settings.repeats < 3:
            run.warnings.insert(0, '快速探索：不能据此宣称最大稳定容量。建议每档至少 60 秒、重复 3 次复核。')
        # State the workload shape explicitly: two runs with the same RPM number but
        # different input lengths answer different questions and must not be compared.
        if settings.dataset_preset and settings.dataset_preset != 'default':
            preset = DATASET_PRESETS.get(settings.dataset_preset, {})
            run.warnings.insert(
                0,
                f'数据集「{preset.get("name", settings.dataset_preset)}」：'
                f'输入约 {settings.input_tokens} token。{preset.get("note", "")}'
                '不同输入长度测出的容量不可直接比较。',
            )
        elif settings.input_tokens is not None:
            run.warnings.insert(
                0,
                f'自定义输入长度约 {settings.input_tokens} token；不同输入长度测出的容量不可直接比较。',
            )
        run.warnings.insert(
            0,
            '所有请求复用同一提示词，可能命中站点前缀或响应缓存；该数字是缓存命中下的上限，'
            '不能当作冷启动容量。'
            if settings.prompt_reuse
            else '每条请求内容都不同（序号与结尾句式），测的是冷启动条件下的容量。',
        )
        needed = required_requests(settings)
        if needed > settings.max_requests:
            run.warnings.insert(
                0,
                f'请求预算 {settings.max_requests} 覆盖不了当前搜索策略（约需 {needed} 个请求）：'
                '会在中途按预算停止，只能得到下界。要么调大预算，要么减少档数。',
            )
        needed_tokens = required_tokens(settings)
        if needed_tokens > settings.token_reserve:
            suggested = math.ceil(needed_tokens / 1_000_000) * 1_000_000
            run.warnings.insert(
                0,
                f'Token 预留 {settings.token_reserve:,} 覆盖不了当前输入长度与档位（约需 {needed_tokens:,}）：'
                f'实验会在中途按预算停止。建议把 token 预留提到 {suggested:,}，或改用更短的输入形态。',
            )
        return self._launch(run, settings.max_wall_seconds, lambda job: self._capacity_work(job, request))

    def start_speed(self, request: SpeedRequest) -> LabRun:
        """Fixed request count at fixed concurrency; reports latency, not capacity."""
        settings = SpeedSettings.model_validate(request.model_dump(exclude={'authorized'}))
        run = LabRun(
            run_id=_id(),
            created_at=datetime.now(timezone.utc).isoformat(),
            kind='speed',
            settings=settings,
            fingerprint=fingerprint(settings),
            warnings=[
                f'固定 {settings.requests} 个请求、并发 {settings.max_parallel}，不做压力搜索。',
                'TTFT 与 TPOT 来自压测引擎的流式统计；分位数样本量等于本次请求数。',
                '结果代表当前网络与站点状态，不能外推为容量上限。',
            ],
        )
        limit = settings.requests * (settings.request_timeout + 5) / max(1, settings.max_parallel) + 120
        return self._launch(run, limit, lambda job: self._speed_work(job, request))

    def start_parallel(self, request: ParallelRequest) -> LabRun:
        """One stage per concurrency level, no search, no SLA verdicts."""
        settings = ParallelSettings.model_validate(request.model_dump(exclude={'authorized'}))
        run = LabRun(
            run_id=_id(),
            created_at=datetime.now(timezone.utc).isoformat(),
            kind='parallel',
            settings=settings,
            fingerprint=fingerprint(settings),
            warnings=[
                f'按指定并发 {", ".join(str(level) for level in settings.levels)} 逐档压测，每档 {settings.stage_seconds} 秒。',
                '并发提高后吞吐下降通常意味着排队或限流，但本页不判断原因。',
                '每档样本量有限，分位数仅作参考；需要稳定结论请提高每档秒数。',
            ],
        )
        limit = len(settings.levels) * (settings.stage_seconds + settings.request_timeout * 2) + 180
        return self._launch(run, limit, lambda job: self._parallel_work(job, request))

    def start_load(self, request: LoadRequest) -> LabRun:
        """Ramp concurrency inside a fixed window and count finished requests.

        Deliberately not a search: every window runs flat out, so the number says how
        much this client could push through at that concurrency, not where the site
        stops. That distinction is written into the run's own warnings.
        """
        settings = LoadSettings.model_validate(request.model_dump(exclude={'authorized'}))
        if settings.concurrency_step == 0 or settings.windows == 1:
            pressure_note = (
                f'恒定并发 {settings.concurrency_start} 路 × {settings.window_seconds} 秒'
                f'（{settings.windows} 个窗口），闭环发送。'
            )
        else:
            pressure_note = (
                f'{settings.windows} 个窗口 × {settings.window_seconds} 秒，'
                f'并发从 {settings.concurrency_start} 起每窗 +{settings.concurrency_step}，闭环发送。'
            )
        run = LabRun(
            run_id=_id(),
            created_at=datetime.now(timezone.utc).isoformat(),
            kind='load',
            settings=settings,
            fingerprint=fingerprint(settings),
            warnings=[
                pressure_note,
                (
                    f'熔断：成功率 < {settings.circuit_success_percent:g}% 或连续失败 '
                    f'≥ {settings.max_consecutive_failures} 或平均响应 > '
                    f'{settings.avg_latency_ms:g} ms 则终止；'
                    f'SLA：成功率 ≥ {settings.success_percent:g}% · '
                    f'TTFT P99 ≤ {settings.ttft_p99_ms:g} ms · '
                    f'TPOT P99 ≤ {settings.tpot_p99_ms:g} ms；'
                    f'建议容量按实测 × {settings.capacity_ratio:g} 折算。'
                ),
                '这里测的是“以该并发一分钟能完成多少请求”；上限由并发与单请求耗时决定，不是站点极限。',
                '只有成功完成的请求计入吞吐；失败单独计数，并在档位理由里标出失败构成。',
                '所有请求复用同一提示词，可能命中缓存；该数字是缓存命中下的上限。'
                if settings.prompt_reuse
                else '每条请求内容都不同（序号与结尾句式），测的是冷启动条件下的吞吐。',
            ],
        )
        limit = settings.duration + settings.windows * (settings.request_timeout + 20) + 120
        return self._launch(run, limit, lambda job: self._load_work(job, request))

    def start_quality(self, request: QualityRequest) -> LabRun:
        """Run the built-in smoke quality suite; grading is local and deterministic."""
        settings = QualitySettings.model_validate(request.model_dump(exclude={'authorized'}))
        items = quality.select(settings)
        run = LabRun(
            run_id=_id(),
            created_at=datetime.now(timezone.utc).isoformat(),
            kind='quality',
            settings=settings,
            fingerprint=fingerprint(settings),
            warnings=[
                f'内置 {len(items)} 道固定题，规则判分，不使用模型当裁判。',
                '这是冒烟质量检查，不是 MMLU 一类基准；不能用来宣称模型综合能力强弱。',
                '判分宽松：允许解释性文字，只提取关键结果；代码题只检查结构，不执行。',
            ],
        )
        # Every item can burn a full timeout, and a slow site will do exactly that.
        rounds = max(1, math.ceil(len(items) / max(1, settings.max_parallel)))
        limit = min(3600.0, rounds * (settings.request_timeout + 5) + 180)
        return self._launch(run, limit, lambda job: self._quality_work(job, request))

    def start_contract(self, request: ContractRequest) -> LabRun:
        """Probe relay fidelity: what the gateway forwards, not how fast it answers."""
        settings = ContractSettings.model_validate(request.model_dump(exclude={'authorized'}))
        checks = contract.select(settings)
        wires = contract.select_protocols(settings)
        planned = contract.plan(settings)
        profile = resolve_profile(settings.model)
        protocol_count = len(wires)
        request_bound = sum(contract.request_cost(check) for _, check in planned) * settings.per_check
        cache_rounds = sum(
            1
            for _, name in planned
            if name in {
                'cache_roundtrip', 'cache_roundtrip_stream',
                'cache_roundtrip_5m', 'cache_roundtrip_5m_stream',
            }
        )
        cache_wait = settings.cache_wait_seconds * settings.per_check * cache_rounds
        protocol_labels = {
            'openai': 'Chat Completions',
            'openai_responses': 'Responses',
            'anthropic': 'Anthropic Messages',
        }
        wire_text = '、'.join(protocol_labels.get(name, name) for name in wires)
        mode_text = (
            '快速模式（核心高信号项 · 跳过 15s 缓存等待）'
            if settings.mode == 'fast'
            else '全量模式（完整 KVV 专项 · 多协议矩阵 · 含缓存闭环）'
        )
        run = LabRun(
            run_id=_id(),
            created_at=datetime.now(timezone.utc).isoformat(),
            kind='contract',
            settings=settings,
            fingerprint=fingerprint(settings),
            warnings=[
                f'检查模式：{mode_text}。',
                f'模型族：{profile.label}（{profile.family}），按该族官方契约自动匹配检查项。',
                f'本次覆盖协议：{wire_text}。',
                f'共 {len(planned)} 项协议适用检查（{len(checks)} 种检查覆盖 {protocol_count} 协议），每项重复 {settings.per_check} 次，最多 {request_bound} 个请求，串行发送。',
                '判定口径：只有“请求被改写”的证据才算违规；站点或模型本身不支持某项能力记为不确定。',
                '缓存检查只认服务端返回的命中计数，不凭耗时推断命中。',
                '本检查验证协议透传，不评价模型回答质量；两者结论不能互相替代。',
            ],
        )
        # Include every stage and the deliberate cache wait in the watchdog budget.
        limit = request_bound * (settings.request_timeout + 5) + cache_wait + 180
        return self._launch(run, limit, lambda job: self._contract_work(job, request))

    def stop(self, run_id: str) -> LabRun:
        """Request cancellation; the watchdog handles subprocess startup races."""
        with self.lock:
            job = self.jobs.get(run_id)
            if job is None:
                raise KeyError(run_id)
            if job.done.is_set():
                return job.run.model_copy(deep=True)
            job.stop.set()
            job.run.status = 'stopping'
            job.run.message = '正在停止，不再派发新档位'
            store.save(self.root, job.run)
            return job.run.model_copy(deep=True)

    def _await_stopping(self, timeout: float = 10.0) -> bool:
        """Wait for jobs that were already asked to stop, so a restart is not a race.

        ``stop()`` returns as soon as cancellation is *requested*; the worker frees its
        slot a moment later. Without this wait, "stop then immediately start" is refused
        with "已有实验运行中", which reads as a lie to anyone who just stopped it.

        Returns True when the caller may proceed, False when a job is genuinely running
        (not stopping) and must still be respected.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                pending = [job for job in self.jobs.values() if not job.done.is_set()]
            if not pending:
                return True
            if any(not job.stop.is_set() for job in pending):
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def _launch(self, run: LabRun, wall_limit: float, work: Callable[[_Job], None]) -> LabRun:
        # Repair unparsable proxy settings before anything builds an HTTP client, and
        # keep a warning on the run so a changed environment is never silent.
        netutil.prepare(run)
        may_start = self._await_stopping()
        with self.lock:
            if any(not job.done.is_set() for job in self.jobs.values()):
                if not may_start:
                    raise RuntimeError('已有实验运行中，请先停止或等待完成')
                raise RuntimeError('上一个实验正在停止但尚未结束，请稍后重试')
            job = _Job(run=run, wall_limit=wall_limit)
            self.jobs[run.run_id] = job
            store.save(self.root, run)
            snapshot = run.model_copy(deep=True)
        threading.Thread(target=self._guard, args=(job, work), daemon=True).start()
        return snapshot

    def _watch(self, job: _Job) -> None:
        while not job.done.wait(0.2):
            if time.monotonic() - job.started >= job.wall_limit:
                with self.lock:
                    job.run.message = '达到总时长安全上限；尚未完成的实验不能视为测到极限'
                    job.stop.set()
            if job.stop.is_set() and job.active_task:
                runner.stop(job.active_task)

    def _guard(self, job: _Job, work: Callable[[_Job], None]) -> None:
        threading.Thread(target=self._watch, args=(job,), daemon=True).start()
        try:
            work(job)
            with self.lock:
                job.run.status = 'stopped' if job.stop.is_set() else 'completed'
                if not job.stop.is_set():
                    # Keep a work-authored summary (e.g. load peak / 本窗实测) when present.
                    current = (job.run.message or '').strip()
                    if not current or current.startswith(_PROGRESS_PREFIXES):
                        job.run.message = DONE_MESSAGES.get(job.run.kind, '完成')
        except runner.ProbeStopped:
            with self.lock:
                job.run.status = 'stopped'
        except Exception as error:
            # Transport exception strings can contain secrets, so the message must stay
            # generic. The *type* does not carry one, and without it every failure looked
            # identical ("执行失败"), which made a bad proxy setting indistinguishable
            # from a dead site. The type plus our own sanitized detail is safe and enough
            # to act on.
            with self.lock:
                job.run.status = 'error'
                job.run.message = (
                    f'执行失败（{type(error).__name__}），请检查连接、协议和本地原生日志；未生成结论。'
                )
                for stage in job.run.stages:
                    if stage.status == 'running':
                        stage.status = 'unknown'
                        stage.reason = '压测进程异常，未获得完整统计'
        finally:
            with self.lock:
                job.active_task = None
                for stage in job.run.stages:
                    if stage.status == 'running':
                        stage.status = 'unknown'
                        stage.reason = '已取消，未获得完整统计'
                for finding in job.run.findings:
                    if finding.status == 'pending':
                        finding.status = 'inconclusive'
                        finding.message = '实验提前结束，本轮未完成，不能推断容量。'
                job.done.set()
                store.save(self.root, job.run)

    # ---------- stage execution ----------

    def _stage(
        self,
        job: _Job,
        *,
        api_key: str | None,
        settings: AnySettings,
        phase: str,
        pressure: int,
        index: int,
        repeat: int,
        number: int,
        parallel: int,
        rate: float,
        duration: float | None,
        max_tokens: int,
        warmup: int,
        verdict: Callable[[LabStage], LabStage],
        circuit_breaker: Callable[[Path], str | None] | None = None,
    ) -> LabStage:
        """Run one stage in a child process and record the observation."""
        stage_started = time.monotonic()
        task_id = f'{job.run.run_id}-{index:03d}'
        stage = LabStage(index=index, phase=phase, pressure=pressure, repeat=repeat, native_task_id=task_id)
        with self.lock:
            job.run.stages.append(stage)
            job.run.message = f'正在测试 {phase.upper()}：第 {index} 档'
            store.save(self.root, job.run)
        directory = self.root / job.run.run_id / f'stage-{index:03d}'
        directory.mkdir(parents=True, exist_ok=True)
        dataset = directory / 'prompts.txt'
        # Only capacity settings carry input_tokens; speed/parallel/contract keep the
        # small built-in prompt, so read it defensively instead of assuming one schema.
        input_tokens = getattr(settings, 'input_tokens', None)
        reuse = bool(getattr(settings, 'prompt_reuse', False))
        dataset.write_text(
            '\n'.join(engine.prompts(phase, number, settings.seed, input_tokens, reuse)) + '\n',
            encoding='utf-8',
        )
        arguments = engine.build(
            model=settings.model,
            url=settings.url,
            protocol=settings.protocol,
            api_key=api_key,
            dataset_path=str(dataset),
            outputs_dir=str(directory / 'native'),
            number=number,
            parallel=parallel,
            rate=rate,
            duration=duration,
            max_tokens=max_tokens,
            timeout=settings.request_timeout,
            seed=settings.seed,
            warmup=warmup,
            stream=bool(getattr(settings, 'stream', True)),
        )
        (directory / 'baseline-args.json').write_text(
            json.dumps(arguments.model_dump(mode='json', exclude={'api_key', 'headers'}), indent=2),
            encoding='utf-8',
        )
        with self.lock:
            job.active_task = task_id
        hard_limit = (duration or 0) + settings.request_timeout * 3 + 180
        database = directory / 'native' / 'benchmark_data.db'
        outcome: dict[str, Any] = {}

        def target() -> None:
            try:
                outcome['value'] = _execute(arguments, task_id=task_id, timeout=hard_limit)
            except BaseException as error:  # re-raised on this thread, not swallowed
                outcome['error'] = error

        # The child process only reports at the end; poll its per-request log meanwhile
        # so a 60 s window shows live counts instead of an empty row.
        worker = threading.Thread(target=target, name=f'stage-{task_id}', daemon=True)
        started = time.monotonic()
        worker.start()
        circuit_tripped: str | None = None
        next_percentiles = 0.0
        while worker.is_alive():
            worker.join(timeout=PROGRESS_INTERVAL)
            if not worker.is_alive():
                break
            total, ok = _read_progress(database)
            sample = _read_live_sample(database)
            elapsed = max(0.001, time.monotonic() - started)
            quantiles = {}
            if phase == 'load' and ok and elapsed >= next_percentiles:
                quantiles = _latency_percentiles(database)
                next_percentiles = elapsed + 3
            with self.lock:
                stage.requests = total
                stage.succeeded = ok
                stage.seconds = round(elapsed, 1)
                if phase == 'load':
                    stage.wall_seconds = round(time.monotonic() - stage_started, 1)
                    stage.token_source = sample.get('token_source', 'unknown')
                stage.rpm = round(ok / elapsed * 60, 1)
                stage.success_percent = round(ok / total * 100, 1) if total else 0.0
                if sample.get('latency_ms'):
                    stage.latency_avg_ms = round(sample['latency_ms'], 1)
                for attr, value in quantiles.items():
                    setattr(stage, attr, value)
                if phase != 'load' and sample.get('ttft_ms'):
                    stage.ttft_p50_ms = round(sample['ttft_ms'], 1)
                if ok and sample.get('completion_tokens'):
                    stage.output_tpm = round(sample['completion_tokens'] / elapsed * 60, 1)
                    stage.mean_output_tokens = round(sample['completion_tokens'] / ok, 1)
                if ok and sample.get('prompt_tokens'):
                    stage.input_tpm = round(sample['prompt_tokens'] / elapsed * 60, 1)
                    stage.mean_input_tokens = round(sample['prompt_tokens'] / ok, 1)
                if phase == 'load' and stage.token_source == 'missing':
                    stage.input_tpm = stage.output_tpm = stage.total_tpm = None
                elif stage.input_tpm is not None and stage.output_tpm is not None:
                    stage.total_tpm = round(stage.input_tpm + stage.output_tpm, 1)
                store.save(self.root, job.run)
            if circuit_breaker is not None and circuit_tripped is None and not job.stop.is_set():
                trip_reason = circuit_breaker(database)
                if trip_reason:
                    circuit_tripped = trip_reason
                    runner.stop(task_id)
        if 'error' in outcome:
            err = outcome['error']
            if phase == 'load' and isinstance(err, runner.ProbeStopped) and (circuit_tripped or job.stop.is_set()):
                observed = _partial_load_window(
                    stage, database, time.monotonic() - started, settings.window_seconds,
                    circuit_tripped or '测试已停止', failed=bool(circuit_tripped),
                )
            else:
                raise err
        elif circuit_tripped:
            observed = verdict(engine.observe(stage, outcome['value']))
            observed.status = 'failed'
            observed.reason = circuit_tripped
        else:
            result = outcome['value']
            observed = verdict(engine.observe(stage, result))
        breakdown = _failure_breakdown(directory / 'native' / 'benchmark_data.db')
        if breakdown:
            observed.failure_reasons = breakdown
            observed.reason = f'{observed.reason}；失败构成：{_format_failure_detail(breakdown)}'
        if observed.requests and observed.succeeded == 0 and not job.stop.is_set():
            if job.diagnosis is None:
                job.diagnosis = diagnose.explain(settings, api_key)
            note = job.diagnosis
            # 只有确实未收到任何 HTTP 状态码响应（或命中超时）时才提示读超时，避免 503 秒拒误报超时。
            has_http_reply = any(key.startswith('HTTP ') for key in observed.failure_reasons)
            if observed.ttft_p50_ms is None and (not has_http_reply or '超时' in observed.failure_reasons):
                note = f'本档在 {settings.request_timeout}s 读超时内没有收到任何数据；{note}'
            observed.reason = f'{observed.reason}；{note}'
        with self.lock:
            job.active_task = None
            if phase == 'load':
                observed.wall_seconds = round(time.monotonic() - stage_started, 1)
            job.run.stages[index - 1] = observed
            store.save(self.root, job.run)
        return observed

    # ---------- speed ----------

    def _speed_work(self, job: _Job, request: SpeedRequest) -> None:
        settings: SpeedSettings = job.run.settings  # type: ignore[assignment]
        key = request.api_key.get_secret_value() if request.api_key else None
        stage = self._stage(
            job,
            api_key=key,
            settings=settings,
            phase='speed',
            pressure=settings.max_parallel,
            index=1,
            repeat=1,
            number=settings.requests,
            parallel=settings.max_parallel,
            rate=-1,
            duration=None,
            max_tokens=settings.output_tokens,
            warmup=min(settings.max_parallel, engine.MAX_WARMUP_REQUESTS),
            verdict=engine.basic_verdict,
        )
        with self.lock:
            job.run.reserved_requests = stage.requests
            job.run.message = (
                f'{stage.succeeded}/{stage.requests} 个请求成功 · TTFT P50 {stage.ttft_p50_ms or 0:.0f} ms'
            )
            store.save(self.root, job.run)

    # ---------- parallel ----------

    def _parallel_work(self, job: _Job, request: ParallelRequest) -> None:
        settings: ParallelSettings = job.run.settings  # type: ignore[assignment]
        key = request.api_key.get_secret_value() if request.api_key else None
        reserved = 0
        for index, level in enumerate(settings.levels, start=1):
            if job.stop.is_set():
                break
            # Enough requests to keep every slot busy for the whole window.
            number = max(3, level * settings.stage_seconds * 2)
            stage = self._stage(
                job,
                api_key=key,
                settings=settings,
                phase='parallel',
                pressure=level,
                index=index,
                repeat=1,
                number=number,
                parallel=level,
                rate=-1,
                duration=settings.stage_seconds,
                max_tokens=settings.output_tokens,
                warmup=min(level, engine.MAX_WARMUP_REQUESTS),
                verdict=engine.basic_verdict,
            )
            reserved += stage.requests
            with self.lock:
                job.run.reserved_requests = reserved
                store.save(self.root, job.run)

    def _load_work(self, job: _Job, request: LoadRequest) -> None:
        """Flat-out windows at rising concurrency; count what actually finished."""
        settings: LoadSettings = job.run.settings  # type: ignore[assignment]
        key = request.api_key.get_secret_value() if request.api_key else None
        best: tuple[int, float] | None = None
        finished = 0
        previous: float | None = None
        saturated = False
        stop_reason: str | None = None
        for index in range(1, settings.windows + 1):
            if job.stop.is_set():
                break
            concurrency = settings.concurrency_start + (index - 1) * settings.concurrency_step
            if concurrency < 1:
                break
            # The dispatch deadline and request budget are the only supply limits.
            number = settings.max_requests - job.run.reserved_requests
            if number < concurrency:
                stop_reason = '请求预算不足以填满并发，提前结束吞吐测试'
                with self.lock:
                    job.run.message = stop_reason
                break
            with self.lock:
                job.run.message = (
                    f'第 {index}/{settings.windows} 个窗口：并发 {concurrency}，{settings.window_seconds} 秒'
                )
            database = self.root / job.run.run_id / f'stage-{index:03d}' / 'native' / 'benchmark_data.db'
            stage = self._stage(
                job,
                api_key=key,
                settings=settings,
                phase='load',
                pressure=concurrency,
                index=index,
                repeat=1,
                number=number,
                parallel=concurrency,
                rate=-1,
                duration=settings.window_seconds,
                max_tokens=settings.output_tokens,
                warmup=0,
                verdict=lambda observed: _finalize_load_window(observed, settings, database, number),
                circuit_breaker=lambda db: _load_circuit_reason(db, settings),
            )
            with self.lock:
                job.run.reserved_requests += stage.requests
                store.save(self.root, job.run)
            finished += stage.succeeded
            if stage.partial_results:
                stop_reason = stage.reason
                break
            if stage.status == 'passed':
                if best is None or stage.rpm > best[1]:
                    best = (concurrency, stage.rpm)
                # Saturation: one more level of concurrency buys almost no throughput,
                # so the client is no longer the bottleneck worth raising.
                metric = stage.output_tpm or stage.rpm or 0.0
                if settings.concurrency_step > 0 and previous and previous > 0 and index < settings.windows:
                    growth = (metric - previous) / previous
                    if growth < settings.saturation_ratio:
                        saturated = True
                        stop_reason = (
                            f'吞吐在并发 {concurrency} 处饱和：较上一档仅增长 {growth * 100:.1f}%，'
                            f'低于 {settings.saturation_ratio * 100:.0f}% 阈值，已停止加压'
                        )
                        break
                previous = metric
            elif stage.status == 'failed' and stage.reason.startswith('熔断'):
                stop_reason = stage.reason
                break
            elif settings.stop_on_failure:
                outcome = '无法确认达标' if stage.status == 'unknown' else '未达标'
                stop_reason = (
                    f'并发 {concurrency} {outcome}'
                    if settings.windows == 1 or settings.concurrency_step == 0
                    else f'并发 {concurrency} {outcome}，已停止继续加压'
                )
                break
        peak_rpm = round(best[1], 1) if best else None
        suggested = round(peak_rpm * settings.capacity_ratio, 1) if peak_rpm is not None else None
        with self.lock:
            job.run.load = LoadSummary(
                peak_concurrency=best[0] if best else None,
                peak_rpm=peak_rpm,
                suggested_rpm=suggested,
                success_total=finished,
                request_total=sum(stage.requests for stage in job.run.stages),
                windows=len(job.run.stages),
                saturated=saturated,
                stop_reason=stop_reason,
            )
            if best:
                suggest_note = (
                    f'建议容量 {suggested:.0f} RPM（×{settings.capacity_ratio:g}）'
                    if suggested is not None
                    else '已跑完全部窗口'
                )
                # Keep message short; stop_reason / stage.reason carry the diagnostic detail.
                job.run.message = (
                    f'峰值窗口：并发 {best[0]}，实测 {best[1]:.0f} RPM；'
                    f'共完成 {finished} 个成功请求。{suggest_note}'
                )
            else:
                # Still surface the best observed window RPM; peak/suggested stay empty.
                measured = (stage for stage in job.run.stages if stage.window_requests is not None)
                observed = max(measured, key=lambda stage: stage.rpm, default=None)
                if observed is not None:
                    job.run.message = (
                        f'本窗实测 {observed.rpm:.0f} RPM（并发 {int(observed.pressure)}），'
                        f'全程 {finished} 个成功请求；该窗口不计入峰值'
                    )
                else:
                    job.run.message = '没有可用窗口数据；请按各档理由检查成功率与失败构成'
            store.save(self.root, job.run)

    # ---------- quality ----------

    def _quality_work(self, job: _Job, request: QualityRequest) -> None:
        settings: QualitySettings = job.run.settings  # type: ignore[assignment]
        key = request.api_key.get_secret_value() if request.api_key else None
        total = len(quality.select(settings))
        with self.lock:
            job.run.message = f'正在运行 {total} 道质量题…'
            store.save(self.root, job.run)

        def on_item(item: QualityItem) -> None:
            """Persist one graded item immediately; the UI polls every few seconds."""
            with self.lock:
                job.run.items.append(item)
                done = len(job.run.items)
                job.run.reserved_requests = done
                job.run.quality = quality.summarize_items(job.run.items)
                job.run.message = f'正在批改 {done}/{total} 题…'
                store.save(self.root, job.run)

        items, summary = quality.run_suite(settings, key, should_stop=job.stop.is_set, on_item=on_item)
        with self.lock:
            # Replace the arrival-ordered list with the stable index order and
            # the full summary (the incremental one lacks token accounting).
            job.run.items = items
            job.run.quality = summary
            job.run.reserved_requests = len(items)
            job.run.message = f'完成 {summary.passed}/{summary.total} 题，准确率 {summary.accuracy}%'
            store.save(self.root, job.run)

    # ---------- contract ----------

    def _contract_work(self, job: _Job, request: ContractRequest) -> None:
        settings: ContractSettings = job.run.settings  # type: ignore[assignment]
        key = request.api_key.get_secret_value() if request.api_key else None
        wires = contract.select_protocols(settings)
        total = len(contract.plan(settings)) * settings.per_check
        with self.lock:
            job.run.message = f'正在执行 {total} 项协议检查（{len(wires)} 协议）…'
            store.save(self.root, job.run)

        def on_item(item: ContractItem) -> None:
            """Persist each verdict immediately; the console receives it over the WebSocket."""
            with self.lock:
                job.run.contract.append(item)
                job.run.reserved_requests += item.requests
                job.run.contract_summary = contract.summarize(job.run.contract)
                job.run.message = f'已完成 {len(job.run.contract)}/{total} 项检查…'
                store.save(self.root, job.run)

        items = contract.run_suite(settings, key, should_stop=job.stop.is_set, on_item=on_item)
        summary = contract.summarize(items)
        with self.lock:
            job.run.contract = items
            job.run.contract_summary = summary
            job.run.reserved_requests = sum(item.requests for item in items)
            obs_part = f'{summary.observation} 项观察、' if summary.observation else ''
            if summary.violated:
                job.run.message = (
                    f'完成 {summary.total} 项：{summary.conformed} 项符合、'
                    f'{summary.violated} 项违反（其中高危 {summary.high_severity} 项）、'
                    f'{obs_part}{summary.inconclusive} 项无法判定。'
                )
            else:
                job.run.message = (
                    f'完成 {summary.total} 项：{summary.conformed} 项符合、'
                    f'{obs_part}{summary.inconclusive} 项无法判定，未发现请求被改写。'
                )
            store.save(self.root, job.run)

    # ---------- capacity ----------

    def _capacity_work(self, job: _Job, request: StartRequest) -> None:
        for finding in job.run.findings:
            if job.stop.is_set():
                break
            self._search(job, request, finding.phase)

    def _search(self, job: _Job, request: StartRequest, phase: Phase) -> None:
        settings: LabSettings = job.run.settings  # type: ignore[assignment]
        ceiling = settings.max_rpm if phase == 'rpm' else settings.max_tpm_parallel
        current = settings.start_rpm if phase == 'rpm' else 1
        # A rate stage can only resolve pressures that still dispatch three requests
        # inside the measured window, so 180 / stage_seconds is the honest floor.
        floor = max(1, math.ceil(180 / settings.stage_seconds)) if phase == 'rpm' else 1
        low: int | None = None
        high: int | None = None
        tested: dict[int, list[LabStage]] = {}
        reservation_start = job.run.reserved_requests
        token_start = job.run.reserved_tokens
        reason = '达到配置的搜索档数或压力上限，尚未证明达到服务极限'
        floor_hit = False
        try:
            for _ in range(settings.max_steps):
                outcome = self._probe(job, request, phase, current, tested, reservation_start, token_start)
                if outcome == 'unknown':
                    reason = '统计缺失或实验被停止，不能可靠推断边界'
                    break
                if outcome == 'latency_floor':
                    floor_hit = True
                    reason = self._floor_reason(tested, current, settings)
                    break
                if outcome == 'failed':
                    high = current
                    reason = '已经观察到不达标档位；不代表限流一定来自中转站'
                    break
                low = current
                if current >= ceiling:
                    break
                current = min(ceiling, current * 2)
            # A strict SLA can fail the very first pressure, which would otherwise leave
            # no passing floor and therefore no number at all. Halve downwards, under the
            # same step budget, until a passing floor is observed.
            for _ in range(settings.max_steps):
                if floor_hit or low is not None or high is None or high <= floor or job.stop.is_set():
                    break
                current = max(floor, high // 2)
                if current >= high:
                    break
                outcome = self._probe(job, request, phase, current, tested, reservation_start, token_start)
                if outcome == 'unknown':
                    reason = '统计缺失或实验被停止，不能可靠推断边界'
                    break
                if outcome == 'latency_floor':
                    floor_hit = True
                    reason = self._floor_reason(tested, current, settings)
                    break
                if outcome == 'passed':
                    low = current
                    reason = '已向下找到达标档位；不代表限流一定来自中转站'
                else:
                    high = current
            for _ in range(settings.refine_steps):
                if floor_hit or low is None or high is None or high - low <= 1 or job.stop.is_set():
                    break
                middle = (low + high) // 2
                outcome = self._probe(job, request, phase, middle, tested, reservation_start, token_start)
                if outcome == 'unknown':
                    reason = '边界复测缺少指标，结论保留不确定性'
                    break
                if outcome == 'passed':
                    low = middle
                else:
                    high = middle
            if low is not None and high is not None:
                reason = f'在 {low} 与 {high} 之间观察到边界；不代表限流一定来自中转站，也不是站点绝对上限'
        except BudgetExhausted:
            reason = '达到本轮请求或估算 Token 预算，未证明达到服务极限'
        candidates = {
            pressure: stages
            for pressure, stages in tested.items()
            if len(stages) == settings.repeats and all(stage.status == 'passed' for stage in stages)
        }
        with self.lock:
            finding = next(item for item in job.run.findings if item.phase == phase)
            if floor_hit:
                finding.status = 'latency_floor'
                finding.message = reason
            elif not candidates:
                finding.status = 'inconclusive'
                finding.message = f'没有完整通过判定的档位。{reason}'
            else:

                def measured(stages: list[LabStage]) -> float:
                    return min(stage.rpm if phase == 'rpm' else stage.output_tpm or 0 for stage in stages)

                best = max(candidates, key=lambda pressure: measured(candidates[pressure]))
                stages = candidates[best]
                finding.value = measured(stages) * settings.capacity_ratio
                finding.pressure = best
                finding.status = 'bounded' if high is not None and high > best else 'lower_bound'
                finding.verified = (
                    settings.repeats >= 3
                    and settings.stage_seconds >= 60
                    and all(
                        stage.succeeded >= 100 and stage.seconds >= settings.stage_seconds * 0.9 for stage in stages
                    )
                )
                finding.message = reason
                if not finding.verified:
                    finding.message = f'短测或样本不足，仅作探索。{reason}'
            store.save(self.root, job.run)

    def _plan(self, job: _Job, phase: Phase, pressure: int, reservation_start: int, token_start: int) -> tuple:
        """Decide how many requests this capacity stage may send, and what it costs."""
        settings: LabSettings = job.run.settings  # type: ignore[assignment]
        phases = [finding.phase for finding in job.run.findings]
        # Split the budget by what each phase actually needs: a 50/50 split starved the
        # rate ladder, which is the expensive one, as soon as both goals were selected.
        weights = {name: max(1, required_requests(settings, [name])) for name in phases}
        share = weights.get(phase, 1)
        total_weight = sum(weights.values()) or 1
        request_left = settings.max_requests * share // total_weight - (job.run.reserved_requests - reservation_start)
        token_left = settings.token_reserve * share // total_weight - (job.run.reserved_tokens - token_start)
        output_limit = settings.short_output_tokens if phase == 'rpm' else settings.long_output_tokens
        # The reservation must follow the *actual* prompt size: a 4096-token input costs
        # ~60x the default, and budgeting on the small built-in prompt would let the run
        # overshoot its declared token ceiling by two orders of magnitude.
        input_reserve = engine.prompt_tokens(phase, settings.seed, settings.input_tokens) * engine.CHARS_PER_TOKEN
        reservation_per_request = input_reserve + 2048 + output_limit
        demand = demand_of(phase, pressure, settings)
        parallel = settings.max_parallel if phase == 'rpm' else pressure
        # Warmup only helps the concurrency ladder, where it fills every slot before the
        # measured burst. In rate mode the paced schedule already spaces requests, and
        # prepending warmup compresses the measured window so the rate reads high.
        affordable = min(request_left, token_left // reservation_per_request) - 3
        warmup = max(0, min(parallel, engine.MAX_WARMUP_REQUESTS, affordable)) if phase == 'tpm' else 0
        count = min(demand, request_left - warmup, token_left // reservation_per_request - warmup)
        if count < 3:
            raise BudgetExhausted
        if phase == 'rpm' and count < demand:
            # Sending fewer paced requests than the window needs ends the stage early,
            # which inflates the measured rate while still claiming stage_seconds.
            raise BudgetExhausted
        return count, warmup, parallel, reservation_per_request

    @staticmethod
    def _floor_reason(tested: dict[int, list[LabStage]], pressure: int, settings: LabSettings) -> str:
        """Explain a latency-floor stop with the measured median, not a generic phrase."""
        stages = tested.get(pressure, [])
        measured = next((stage for stage in reversed(stages) if stage.ttft_p50_ms is not None), None)
        p50 = f'{measured.ttft_p50_ms:.0f} ms' if measured else '未记录'
        return (
            f'TTFT 地板高于设定上限：{pressure} 档 P50 已达 {p50}，'
            f'超过阈值 {settings.ttft_p99_ms:.0f} ms；降速不会改善延迟，无法在此 SLA 下测量容量。'
            '请放宽 ttft_p99_ms 或更换站点后重测。'
        )

    def _probe(
        self,
        job: _Job,
        request: StartRequest,
        phase: Phase,
        pressure: int,
        tested: dict[int, list[LabStage]],
        reservation_start: int,
        token_start: int,
    ) -> str:
        settings: LabSettings = job.run.settings  # type: ignore[assignment]
        key = request.api_key.get_secret_value() if request.api_key else None
        tested[pressure] = []
        for repeat in range(1, settings.repeats + 1):
            if job.stop.is_set():
                return 'unknown'
            if job.run.stages and job.stop.wait(settings.cooldown_seconds):
                return 'unknown'
            count, warmup, parallel, per_request = self._plan(job, phase, pressure, reservation_start, token_start)
            with self.lock:
                job.run.reserved_requests += count + warmup
                job.run.reserved_tokens += (count + warmup) * per_request
            observed = self._stage(
                job,
                api_key=key,
                settings=settings,
                phase=phase,
                pressure=pressure,
                index=len(job.run.stages) + 1,
                repeat=repeat,
                number=count,
                parallel=parallel,
                rate=-1 if phase == 'tpm' else pressure / 60,
                duration=settings.stage_seconds,
                max_tokens=settings.short_output_tokens if phase == 'rpm' else settings.long_output_tokens,
                warmup=warmup,
                verdict=lambda stage: engine.judge(stage, settings),
            )
            tested[pressure].append(observed)
            if observed.status != 'passed':
                # A failed latency floor is not a capacity boundary: report it as its
                # own outcome so the search stops instead of halving into the floor.
                if latency_floor(observed, settings):
                    return 'latency_floor'
                return observed.status
        return 'passed'
