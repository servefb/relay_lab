"""Measurement adapter over the vendored load generator.

The load generator itself lives in :mod:`relay_lab.vendor` (copied from
EvalScope, Apache-2.0): rate/concurrency scheduling, SSE streaming, TTFT/TPOT
percentiles and token accounting. This module only builds its Arguments, runs
one job in a child process and turns the summary into our own stage model.
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from relay_lab.vendor.perf.arguments import Arguments
from relay_lab.vendor.perf.benchmark import run_benchmark
from relay_lab.vendor.perf.utils.perf_models import BenchmarkSummary, PercentileResult

from .config import LabSettings, LabStage
from .window import Completion

MIN_JUDGE_SAMPLES = 30
"""Below this, a percentile is just the maximum, so a pass is only exploratory."""

MAX_WARMUP_REQUESTS = 16
"""Upper bound on per-stage warmup traffic so a large concurrency limit cannot inflate cost."""

INSTRUCTIONS = {
    'rpm': 'Classify the sentiment as positive, neutral, or negative. Answer with one word. Text: ',
    'speed': 'Explain in one short paragraph why streaming responses feel faster than batch responses. ',
    'tpm': 'Write a detailed practical guide to keeping a daily journal. Continue until the output limit. ',
    # Throughput mode wants the short RPM-style task: output length is a setting there,
    # not a property of the prompt, so reusing the TPM instruction would skew the count.
    'load': 'Classify the sentiment as positive, neutral, or negative. Answer with one word. Text: ',
}
TAILS = {
    'rpm': 'The delivery arrived on time.',
    'speed': 'Keep it under 200 words.',
    'tpm': 'Include steps, examples and common mistakes.',
    'load': 'The delivery arrived on time.',
}
TAIL_POOLS = {
    # Distinct sentences so two requests are not byte-identical beyond the case id.
    # A workload whose bodies only differ by a number still invites gateway-side
    # dedupe/caching on content, and it cannot be described as "different requests".
    'rpm': (
        'The delivery arrived on time.',
        'The package was damaged on arrival.',
        'The driver was polite and quick.',
        'It showed up without any notice.',
        'Support solved the problem in minutes.',
        'The refund took three full weeks.',
    ),
    'load': (
        'The delivery arrived on time.',
        'The package was damaged on arrival.',
        'The driver was polite and quick.',
        'It showed up without any notice.',
        'Support solved the problem in minutes.',
        'The refund took three full weeks.',
    ),
    'speed': (
        'Keep it under 200 words.',
        'Use one concrete example.',
        'Avoid jargon where possible.',
        'Mention a single trade-off.',
    ),
    'tpm': (
        'Include steps, examples and common mistakes.',
        'Cover beginners and advanced readers.',
        'Add a short daily template.',
        'Explain how to keep the habit.',
        'Mention the usual pitfalls.',
    ),
}

def endpoint_of(url: str) -> str:
    """Add the version prefix only when the address has no path of its own."""
    return url + '/v1' if not urlsplit(url).path.rstrip('/') else url


CHARS_PER_TOKEN = 4
"""Deliberately conservative for English filler (~4 chars/token); CJK filler is ~1.5.

Padding text here is ASCII, so 4 is the closest simple constant. The point is a
*repeatable* length, not an exact tokenizer match, and the preset labels say "约".
"""

PAD_SENTENCES = (
    'The following reference material exists only to occupy context; ignore its content. ',
    'Section {index}: a maintenance log records routine checks with no unusual findings. ',
    'In this passage the author restates the same neutral background several times over. ',
    'Nothing in this text is a question, and no part of it should be answered or quoted. ',
)


def pad_to_tokens(target: int) -> str:
    """Build single-line filler of roughly *target* tokens.

    Single-line is a hard requirement: the custom dataset plugin reads the prompt file
    line by line, so an embedded newline would silently split one prompt into several
    shorter ones and change the workload without any error.
    """
    if target <= 0:
        return ''
    wanted_chars = target * CHARS_PER_TOKEN
    parts: list[str] = []
    length = 0
    index = 0
    while length < wanted_chars:
        sentence = PAD_SENTENCES[index % len(PAD_SENTENCES)].format(index=index)
        parts.append(sentence)
        length += len(sentence)
        index += 1
    return ''.join(parts)[:wanted_chars]


def prompts(
    phase: str,
    count: int,
    seed: int,
    input_tokens: int | None = None,
    reuse: bool = False,
) -> list[str]:
    """Deterministic local workload; nothing is downloaded or cached between runs.

    When ``input_tokens`` is set the filler is inserted *after* the instruction, so the
    model still receives a real task at the start of a long context rather than a wall of
    noise it can ignore entirely.

    ``reuse`` collapses every entry onto the first prompt, which is the only honest way
    to ask "what does this site do when the prefix cache hits". With ``reuse=False`` the
    prompts differ in both the case id and the closing sentence, so a run really is a
    stream of different requests rather than one request repeated with a counter.
    """
    instruction = INSTRUCTIONS.get(phase, INSTRUCTIONS['tpm'])
    pool = TAIL_POOLS.get(phase, TAIL_POOLS['tpm'])
    filler = pad_to_tokens(max(0, input_tokens - 24)) if input_tokens is not None else ''

    def build(index: int) -> str:
        if reuse:
            index = 0
        tail = pool[index % len(pool)]
        head = f'Case {seed}-{index:06d}. {instruction}'
        return f'{head}{filler} {tail}' if filler else f'{head}{tail}'

    return [build(index) for index in range(count)]


def prompt_tokens(phase: str, seed: int, input_tokens: int | None = None) -> int:
    """Estimated input tokens for one prompt, used for budget reservation."""
    sample = prompts(phase, 1, seed, input_tokens)[0]
    return max(1, len(sample.encode('utf-8')) // CHARS_PER_TOKEN)


def build(
    *,
    model: str,
    url: str,
    protocol: str,
    api_key: str | None,
    dataset_path: str,
    outputs_dir: str,
    number: int,
    parallel: int,
    rate: float,
    duration: float,
    max_tokens: int,
    timeout: int,
    seed: int,
    warmup: int = 0,
    stream: bool = True,
) -> Arguments:
    """Translate one stage into an EvalScope perf job."""
    return Arguments(
        model=model,
        # 按批提交并配合消费端 0.5s 定时刷盘，兼顾高并发性能与实时进度刷新。
        db_commit_interval=20,
        url=endpoint_of(url),
        api=protocol,
        api_key=api_key,
        dataset='custom',
        dataset_path=dataset_path,
        apply_chat_template=True,
        number=number,
        parallel=parallel,
        rate=rate,
        duration=duration,
        max_tokens=max_tokens,
        # Kimi / GLM thinking routes reject values other than 1.
        temperature=1.0,
        stream=stream,
        warmup_num=warmup,
        no_test_connection=True,
        total_timeout=timeout,
        connect_timeout=min(10, timeout),
        read_timeout=timeout,
        outputs_dir=outputs_dir,
        no_timestamp=True,
        sleep_interval=0,
        seed=seed,
        debug=False,
    )


def _scalar(value: Any) -> Any:
    """``Arguments`` normalises sweep fields to lists; a single run wants one value."""
    return value[0] if isinstance(value, (list, tuple)) and value else value


def run_native(arguments: Arguments) -> Any:
    """Top-level entry point: it has to be picklable for the spawn context."""
    job = arguments.model_copy(deep=True)
    job.number = _scalar(job.number)
    job.parallel = _scalar(job.parallel)
    job.rate = _scalar(job.rate)
    # Upstream's sweep loop creates this directory per run; a single run must too,
    # otherwise the per-request SQLite log has nowhere to go.
    Path(job.outputs_dir).mkdir(parents=True, exist_ok=True)
    return asyncio.run(run_benchmark(job))


def _finite(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value) if value >= 0 else None
    return None


def _stream_ms(value: Any, streamed: int) -> float | None:
    """TTFT only counts when the run actually received SSE and a positive first byte."""
    if not streamed:
        return None
    number = _finite(value)
    if number is None or number <= 0:
        return None
    return number


def _row(percentiles: PercentileResult, name: str) -> Any:
    return next((row for row in percentiles.rows if row.percentile == name), None)


def observe(stage: LabStage, result: Any) -> LabStage:
    """Read one load-generator result into per-minute units, with no verdict yet."""
    # The child process returns the tuple through a queue, which serialises it to a list.
    if not isinstance(result, (tuple, list)) or len(result) != 4:
        raise ValueError('Expected (summary, percentiles, trace, workload) from the runner')
    summary, percentiles, _trace, _workload = result
    summary = BenchmarkSummary.model_validate(summary)
    percentiles = PercentileResult.model_validate(percentiles)
    if summary.total_requests < 0 or not 0 <= summary.succeed_requests <= summary.total_requests:
        raise ValueError('Invalid native request counters')
    seconds = summary.time_taken
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('Native benchmark returned an invalid duration')
    p50 = _row(percentiles, '50%')
    p90 = _row(percentiles, '90%')
    p95 = _row(percentiles, '95%')
    p99 = _row(percentiles, '99%')
    observed = stage.model_copy(deep=True)
    observed.requests = summary.total_requests
    observed.succeeded = summary.succeed_requests
    observed.seconds = seconds
    observed.success_percent = 100 * observed.succeeded / observed.requests if observed.requests else 0
    observed.rpm = observed.succeeded / seconds * 60
    # P95 alongside P99: P99 on a short stage is often just the maximum, so P95 is the
    # more stable capacity signal. The engine computes both; reading only P99 threw half
    # the available distribution away.
    # A stored 0 is the unset default, not a measured first token. Non-stream
    # replies also have no first-byte sample; don't publish that as TTFT.
    streamed = getattr(summary, 'stream_requests', 0) or 0
    observed.ttft_p50_ms = _stream_ms(p50.ttft if p50 else None, streamed)
    observed.ttft_p95_ms = _stream_ms(p95.ttft if p95 else None, streamed)
    observed.ttft_p99_ms = _stream_ms(p99.ttft if p99 else None, streamed)
    observed.tpot_p50_ms = _finite(p50.tpot) if (streamed and p50) else None
    observed.tpot_p95_ms = _finite(p95.tpot) if (streamed and p95) else None
    observed.tpot_p99_ms = _finite(p99.tpot) if (streamed and p99) else None
    average = _finite(summary.avg_latency)
    observed.latency_avg_ms = average * 1000 if average else None
    lat50 = _finite(p50.latency) if p50 else None
    observed.latency_p50_ms = lat50 * 1000 if lat50 else None
    lat90 = _finite(p90.latency) if p90 else None
    observed.latency_p90_ms = lat90 * 1000 if lat90 else None
    mid = _finite(p95.latency) if p95 else None
    observed.latency_p95_ms = mid * 1000 if mid else None
    tail = _finite(p99.latency) if p99 else None
    observed.latency_p99_ms = tail * 1000 if tail else None
    observed.mean_input_tokens = _finite(summary.avg_input_tokens)
    observed.mean_output_tokens = _finite(summary.avg_output_tokens)
    # Achieved concurrency by Little's Law: L = throughput x latency. Reported as a
    # measurement (it can be lower than requested when the site queues), never as the
    # requested target.
    if average and observed.succeeded:
        throughput = observed.succeeded / seconds
        observed.mean_concurrency = round(throughput * average, 2)
    observed.peak_concurrency = _finite(summary.concurrency)
    if observed.succeeded and summary.avg_output_tokens > 0:
        observed.output_tpm = summary.avg_output_tokens * observed.succeeded / seconds * 60
    if observed.succeeded and summary.avg_input_tokens > 0:
        observed.input_tpm = summary.avg_input_tokens * observed.succeeded / seconds * 60
    if observed.input_tpm is not None and observed.output_tpm is not None:
        observed.total_tpm = observed.input_tpm + observed.output_tpm
    return observed


def judge(observed: LabStage, settings: LabSettings) -> LabStage:
    """Apply the capacity SLA: success rate, TTFT P99 and TPOT P99."""
    if not observed.requests or observed.success_percent < settings.success_percent:
        observed.status = 'failed'
        observed.reason = (
            f'成功率 {observed.success_percent:.1f}% 低于要求 {settings.success_percent:g}%；检查该档原生日志'
        )
        return observed
    if observed.ttft_p99_ms is None or (observed.phase == 'tpm' and observed.tpot_p99_ms is None):
        observed.status = 'unknown'
        observed.reason = '缺少必要的流式延迟指标，不能判为达标'
        return observed
    if observed.phase == 'tpm' and observed.output_tpm is None:
        observed.status = 'unknown'
        observed.reason = '缺少输出 Token 统计，不能推断 TPM'
        return observed
    if observed.ttft_p99_ms > settings.ttft_p99_ms:
        observed.status = 'failed'
        observed.reason = '正文 TTFT P99 超过设定上限'
        return observed
    if observed.phase == 'tpm' and observed.tpot_p99_ms > settings.tpot_p99_ms:
        observed.status = 'failed'
        observed.reason = 'TPOT P99 超过设定上限'
        return observed
    observed.status = 'passed'
    if observed.succeeded >= 100:
        observed.reason = '成功率与延迟达标'
    elif observed.requests < MIN_JUDGE_SAMPLES:
        observed.reason = f'本档达标，但只有 {observed.requests} 个样本，P99 等于最大值，仅作探索'
    else:
        observed.reason = '本档达标；样本较少，P99 仅作参考'
    return observed


def summarize(stage: LabStage, result: Any, settings: LabSettings) -> LabStage:
    """Observe and judge in one call: this is the capacity path."""
    return judge(observe(stage, result), settings)


def completions(db_path: str | Path) -> list[Completion]:
    """Read the engine's per-request log into sliding-window events.

    The native run writes one SQLite row per request with its completion time and token
    counts. Stage averages cannot show whether throughput held steady or collapsed
    mid-stage, so the raw rows are read back and turned into a real time series.

    Only successful rows count: a failed request moved no tokens, and counting it would
    overstate throughput. Unreadable or absent logs yield an empty list — an absent
    series is reported as absent, never replaced by a fabricated flat line.
    """
    path = Path(db_path)
    if not path.is_file():
        return []
    events: list[Completion] = []
    try:
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
            cursor = connection.execute(
                'SELECT completed_time, prompt_tokens, completion_tokens FROM result '
                'WHERE success = 1 AND completed_time IS NOT NULL ORDER BY completed_time'
            )
            for completed, prompt, completion in cursor.fetchall():
                moment = _finite(completed)
                if moment is None:
                    continue
                events.append(
                    Completion(
                        at=float(moment),
                        prompt_tokens=int(_finite(prompt) or 0),
                        completion_tokens=int(_finite(completion) or 0),
                    )
                )
    except sqlite3.Error:
        return []
    return events


def basic_verdict(observed: LabStage) -> LabStage:
    """Speed/parallel stages report facts; only success and presence of metrics gate them."""
    if not observed.requests:
        observed.status = 'unknown'
        observed.reason = '没有完成的请求'
    elif observed.success_percent < 99:
        observed.status = 'failed'
        observed.reason = f'成功率 {observed.success_percent:.1f}% 低于 99%；检查该档原生日志'
    elif observed.ttft_p99_ms is None:
        observed.status = 'unknown'
        observed.reason = '缺少 TTFT 指标，不能计算速度'
    else:
        observed.status = 'passed'
        observed.reason = '本档请求全部成功' if observed.succeeded >= 20 else '样本较少，分位数仅作参考'
    return observed
