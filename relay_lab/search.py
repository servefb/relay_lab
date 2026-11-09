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
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from . import contract, diagnose, engine, netutil, quality, runner, store
from .config import (
    ContractItem,
    ContractRequest,
    ContractSettings,
    LabFinding,
    LabRun,
    LabSettings,
    LabStage,
    ParallelRequest,
    ParallelSettings,
    QualityItem,
    QualityRequest,
    QualitySettings,
    SpeedRequest,
    SpeedSettings,
    StartRequest,
)

Phase = Literal['rpm', 'tpm']
AnySettings = LabSettings | SpeedSettings | ParallelSettings | QualitySettings | ContractSettings

DONE_MESSAGES = store.DONE_MESSAGES


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
        needed = required_requests(settings)
        if needed > settings.max_requests:
            run.warnings.insert(
                0,
                f'请求预算 {settings.max_requests} 覆盖不了当前搜索策略（约需 {needed} 个请求）：'
                '会在中途按预算停止，只能得到下界。要么调大预算，要么减少档数。',
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
        run = LabRun(
            run_id=_id(),
            created_at=datetime.now(timezone.utc).isoformat(),
            kind='contract',
            settings=settings,
            fingerprint=fingerprint(settings),
            warnings=[
                f'共 {len(checks)} 项检查，串行发送，每项约 1~2 个请求；总量远小于容量测试。',
                '判定口径：只有“请求被改写”的证据才算违规；站点或模型本身不支持某项能力记为不确定。',
                '缓存检查只认服务端返回的命中计数，不凭耗时推断命中。',
                '本检查验证协议透传，不评价模型回答质量；两者结论不能互相替代。',
            ],
        )
        # The cache probe sends two full long-prefix requests; give the run room for them.
        limit = min(1800.0, len(checks) * (settings.request_timeout + 5) * 2 + 180)
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
    ) -> LabStage:
        """Run one stage in a child process and record the observation."""
        task_id = f'{job.run.run_id}-{index:03d}'
        stage = LabStage(index=index, phase=phase, pressure=pressure, repeat=repeat, native_task_id=task_id)
        with self.lock:
            job.run.stages.append(stage)
            job.run.message = f'正在测试 {phase.upper()}：第 {index} 档'
            store.save(self.root, job.run)
        directory = self.root / job.run.run_id / f'stage-{index:03d}'
        directory.mkdir(parents=True, exist_ok=True)
        dataset = directory / 'prompts.txt'
        dataset.write_text('\n'.join(engine.prompts(phase, number, settings.seed)) + '\n', encoding='utf-8')
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
        )
        (directory / 'baseline-args.json').write_text(
            json.dumps(arguments.model_dump(mode='json', exclude={'api_key', 'headers'}), indent=2),
            encoding='utf-8',
        )
        with self.lock:
            job.active_task = task_id
        hard_limit = (duration or 0) + settings.request_timeout * 3 + 180
        result = _execute(arguments, task_id=task_id, timeout=hard_limit)
        observed = verdict(engine.observe(stage, result))
        if observed.requests and observed.succeeded == 0 and not job.stop.is_set():
            if job.diagnosis is None:
                job.diagnosis = diagnose.explain(settings, api_key)
            note = job.diagnosis
            if observed.ttft_p50_ms is None:
                note = f'本档在 {settings.request_timeout}s 读超时内没有收到任何数据；{note}'
            observed.reason = f'{observed.reason}；{note}'
        with self.lock:
            job.active_task = None
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
        total = len(contract.select(settings))
        with self.lock:
            job.run.message = f'正在执行 {total} 项协议检查…'
            store.save(self.root, job.run)

        def on_item(item: ContractItem) -> None:
            """Persist each verdict immediately; the UI polls every few seconds."""
            with self.lock:
                job.run.contract.append(item)
                job.run.reserved_requests += 1
                job.run.contract_summary = contract.summarize(job.run.contract)
                job.run.message = f'已完成 {len(job.run.contract)}/{total} 项检查…'
                store.save(self.root, job.run)

        items = contract.run_suite(settings, key, should_stop=job.stop.is_set, on_item=on_item)
        summary = contract.summarize(items)
        with self.lock:
            job.run.contract = items
            job.run.contract_summary = summary
            job.run.reserved_requests = len(items)
            if summary.violated:
                job.run.message = (
                    f'完成 {summary.total} 项：{summary.conformed} 项符合、'
                    f'{summary.violated} 项违反（其中高危 {summary.high_severity} 项）、'
                    f'{summary.inconclusive} 项无法判定。'
                )
            else:
                job.run.message = (
                    f'完成 {summary.total} 项：{summary.conformed} 项符合、'
                    f'{summary.inconclusive} 项无法判定，未发现请求被改写。'
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
        try:
            for _ in range(settings.max_steps):
                outcome = self._probe(job, request, phase, current, tested, reservation_start, token_start)
                if outcome == 'unknown':
                    reason = '统计缺失或实验被停止，不能可靠推断边界'
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
                if low is not None or high is None or high <= floor or job.stop.is_set():
                    break
                current = max(floor, high // 2)
                if current >= high:
                    break
                outcome = self._probe(job, request, phase, current, tested, reservation_start, token_start)
                if outcome == 'unknown':
                    reason = '统计缺失或实验被停止，不能可靠推断边界'
                    break
                if outcome == 'passed':
                    low = current
                    reason = '已向下找到达标档位；不代表限流一定来自中转站'
                else:
                    high = current
            for _ in range(settings.refine_steps):
                if low is None or high is None or high - low <= 1 or job.stop.is_set():
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
            if not candidates:
                finding.status = 'inconclusive'
                finding.message = f'没有完整通过判定的档位。{reason}'
            else:

                def measured(stages: list[LabStage]) -> float:
                    return min(stage.rpm if phase == 'rpm' else stage.output_tpm or 0 for stage in stages)

                best = max(candidates, key=lambda pressure: measured(candidates[pressure]))
                stages = candidates[best]
                finding.value = measured(stages)
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
        reservation_per_request = len(engine.prompts(phase, 1, settings.seed)[0].encode('utf-8')) + 2048 + output_limit
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
                return observed.status
        return 'passed'
