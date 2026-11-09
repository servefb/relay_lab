"""Typed contracts for the relay capacity lab.

Every model here is ours. EvalScope is only used as a load-generation library,
so nothing in this module depends on its service layer or its web contracts.
"""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

Phase = Literal['rpm', 'tpm', 'speed', 'parallel', 'quality', 'contract']
RunKind = Literal['capacity', 'speed', 'parallel', 'quality', 'contract']

QUALITY_CATEGORIES: tuple[str, ...] = ('算术', '指令', 'JSON', '中文理解', '代码', '逻辑推理', '多模态')
"""The only category names the built-in suite can serve; a typo must fail loudly."""

CONTRACT_CHECKS: tuple[str, ...] = (
    'system',
    'tools',
    'json_schema',
    'max_tokens',
    'stop',
    'multi_turn',
    'errors',
    'stream',
    'cache',
)
"""Relay-fidelity probes: does the gateway forward what it was sent?

These are protocol questions, not model questions. A relay that drops ``tools``, ignores
``stop``, buffers a stream into one burst or rewrites ``max_tokens`` still answers
plausibly, which is exactly why each probe is built to fail loudly when the request was
altered. An unknown name would silently probe nothing, so it is rejected.
"""


class Contract(BaseModel):
    """Reject unknown fields so a typo cannot silently change an experiment."""

    model_config = ConfigDict(extra='forbid', populate_by_name=True)


class ConnectionSettings(Contract):
    """What a site needs: address, protocol and model."""

    label: str = Field(default='未命名实验', min_length=1, max_length=80)
    url: str = Field(min_length=8, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    protocol: Literal['openai', 'openai_responses', 'anthropic', 'gemini'] = 'openai'
    anthropic_version: str = Field(default='2023-06-01', min_length=1, max_length=32)
    """Sent as ``anthropic-version``; some relays pin an older date than the default."""

    @field_validator('url')
    @classmethod
    def validate_url(cls, value: str) -> str:
        """Reject credentials in URLs and protocol-relative endpoints."""
        value = value.strip().rstrip('/')
        parts = urlsplit(value)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError('请输入完整 HTTP(S) API 地址')
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('地址不能包含账号、密码或查询参数；密钥请填 API Key')
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError('端口不合法')
        return value

    @model_validator(mode='after')
    def validate_endpoint(self) -> 'ConnectionSettings':
        """Keep the declared protocol and an explicit endpoint consistent.

        A mismatched pair would send an Anthropic body to a Chat Completions route and
        produce a confusing upstream error, so it is rejected here instead.
        """
        path = urlsplit(self.url).path.rstrip('/')
        expected: dict[str, str] = {
            '/responses': 'openai_responses',
            '/chat/completions': 'openai',
            '/messages': 'anthropic',
        }
        for suffix, protocol in expected.items():
            if path.endswith(suffix) and self.protocol != protocol:
                raise ValueError(f'{suffix} 端点需要选择对应的 {protocol} 协议')
        if ':generateContent' in path or ':streamGenerateContent' in path:
            if self.protocol != 'gemini':
                raise ValueError('Gemini 端点需要选择 Gemini 协议')
        if not self.model.strip():
            raise ValueError('模型名称不能为空')
        return self


class LabSettings(ConnectionSettings):
    """Reproducible, non-secret workload and safety settings for a capacity run."""

    goal: Literal['both', 'rpm', 'tpm'] = 'both'
    stage_seconds: int = Field(default=30, ge=5, le=600, strict=True)
    repeats: int = Field(default=1, ge=1, le=3, strict=True)
    max_steps: int = Field(default=5, ge=1, le=8, strict=True)
    refine_steps: int = Field(default=3, ge=0, le=5, strict=True)
    start_rpm: int = Field(default=60, ge=1, le=6000, strict=True)
    max_rpm: int = Field(default=480, ge=1, le=6000, strict=True)
    max_parallel: int = Field(default=8, ge=1, le=128, strict=True)
    max_tpm_parallel: int = Field(default=16, ge=1, le=128, strict=True)
    max_requests: int = Field(default=1000, ge=3, le=20000, strict=True)
    token_reserve: int = Field(default=3000000, ge=10000, le=100000000, strict=True)
    max_wall_seconds: int = Field(default=1200, ge=30, le=7200, strict=True)
    request_timeout: int = Field(default=60, ge=2, le=300, strict=True)
    cooldown_seconds: int = Field(default=10, ge=0, le=120, strict=True)
    success_percent: float = Field(default=99, ge=90, le=100, allow_inf_nan=False)
    ttft_p99_ms: float = Field(default=3000, gt=0, le=120000, allow_inf_nan=False)
    tpot_p99_ms: float = Field(default=100, gt=0, le=10000, allow_inf_nan=False)
    short_output_tokens: int = Field(default=32, ge=8, le=256, strict=True)
    long_output_tokens: int = Field(default=1024, ge=64, le=4096, strict=True)
    seed: int = Field(default=42, ge=0, le=2147483647, strict=True)

    @model_validator(mode='after')
    def validate_bounds(self) -> 'LabSettings':
        if self.start_rpm > self.max_rpm:
            raise ValueError('起始 RPM 不能高于 RPM 安全上限')
        return self


class SpeedSettings(ConnectionSettings):
    """Single-stream latency check: fixed request count, fixed concurrency."""

    requests: int = Field(default=20, ge=1, le=500, strict=True)
    max_parallel: int = Field(default=1, ge=1, le=8, strict=True)
    output_tokens: int = Field(default=256, ge=8, le=4096, strict=True)
    request_timeout: int = Field(default=60, ge=2, le=300, strict=True)
    seed: int = Field(default=42, ge=0, le=2147483647, strict=True)


class ParallelSettings(ConnectionSettings):
    """Concurrency ladder: one stage per requested level."""

    levels: list[int] = Field(default_factory=lambda: [1, 2, 4, 8], min_length=1, max_length=8)
    stage_seconds: int = Field(default=15, ge=5, le=300, strict=True)
    output_tokens: int = Field(default=256, ge=8, le=4096, strict=True)
    request_timeout: int = Field(default=60, ge=2, le=300, strict=True)
    seed: int = Field(default=42, ge=0, le=2147483647, strict=True)

    @field_validator('levels')
    @classmethod
    def validate_levels(cls, value: list[int]) -> list[int]:
        """Unique, ordered, and inside the concurrency range the runner supports."""
        cleaned = sorted({level for level in value})
        if not cleaned:
            raise ValueError('至少需要一个并发档位')
        if cleaned[0] < 1 or cleaned[-1] > 128:
            raise ValueError('并发档位必须在 1 到 128 之间')
        return cleaned


class QualitySettings(ConnectionSettings):
    """Built-in smoke quality suite with deterministic grading."""

    categories: list[str] = Field(default_factory=list)
    per_category: int = Field(default=8, ge=1, le=20, strict=True)
    max_parallel: int = Field(default=1, ge=1, le=8, strict=True)
    request_timeout: int = Field(default=60, ge=2, le=300, strict=True)

    @field_validator('categories')
    @classmethod
    def validate_categories(cls, value: list[str]) -> list[str]:
        """An unknown name would silently select zero items and report 0%."""
        unknown = [name for name in value if name not in QUALITY_CATEGORIES]
        if unknown:
            allowed = '、'.join(QUALITY_CATEGORIES)
            raise ValueError(f'未知的题目分类 {unknown}；可选：{allowed}')
        return list(dict.fromkeys(value))


class StartRequest(LabSettings):
    """Explicit authorization is required before any benchmark traffic."""

    api_key: SecretStr | None = Field(default=None, exclude=True)
    authorized: Literal[True]

    @field_validator('authorized', mode='before')
    @classmethod
    def require_explicit_authorization(cls, value: object) -> bool:
        """Reject truthy strings and integers as authorization."""
        if value is not True:
            raise ValueError('必须明确确认测试授权与预算')
        return True


class SpeedRequest(SpeedSettings):
    """Speed test plus the transient credential and consent flag."""

    api_key: SecretStr | None = Field(default=None, exclude=True)
    authorized: Literal[True]

    @field_validator('authorized', mode='before')
    @classmethod
    def require_explicit_authorization(cls, value: object) -> bool:
        return StartRequest.require_explicit_authorization(value)


class ParallelRequest(ParallelSettings):
    """Parallel test plus the transient credential and consent flag."""

    api_key: SecretStr | None = Field(default=None, exclude=True)
    authorized: Literal[True]

    @field_validator('authorized', mode='before')
    @classmethod
    def require_explicit_authorization(cls, value: object) -> bool:
        return StartRequest.require_explicit_authorization(value)


class QualityRequest(QualitySettings):
    """Quality check plus the transient credential and consent flag."""

    api_key: SecretStr | None = Field(default=None, exclude=True)
    authorized: Literal[True]

    @field_validator('authorized', mode='before')
    @classmethod
    def require_explicit_authorization(cls, value: object) -> bool:
        return StartRequest.require_explicit_authorization(value)


class ContractSettings(ConnectionSettings):
    """Relay-fidelity probes: a cheap suite that checks what the gateway forwards."""

    checks: list[str] = Field(default_factory=list)
    per_check: int = Field(default=1, ge=1, le=3, strict=True)
    max_parallel: int = Field(default=1, ge=1, le=8, strict=True)
    request_timeout: int = Field(default=60, ge=2, le=300, strict=True)
    cache_prefix_tokens: int = Field(default=2048, ge=512, le=8192, strict=True)

    @field_validator('checks')
    @classmethod
    def validate_checks(cls, value: list[str]) -> list[str]:
        """Reject an unknown probe name instead of silently running fewer checks."""
        unknown = [name for name in value if name not in CONTRACT_CHECKS]
        if unknown:
            allowed = '、'.join(CONTRACT_CHECKS)
            raise ValueError(f'未知的契约检查 {unknown}；可选：{allowed}')
        return list(dict.fromkeys(value))


class ContractRequest(ContractSettings):
    """Contract probes plus the transient credential and consent flag."""

    api_key: SecretStr | None = Field(default=None, exclude=True)
    authorized: Literal[True]

    @field_validator('authorized', mode='before')
    @classmethod
    def require_explicit_authorization(cls, value: object) -> bool:
        return StartRequest.require_explicit_authorization(value)


class ContractItem(Contract):
    """One probe: what was asked, what came back, and what that proves.

    ``verdict`` is deliberately three-valued. ``inconclusive`` exists because a relay can
    legitimately refuse a feature (a text-only model cannot serve images, a site may not
    implement ``json_schema``); calling that a failure would be a false accusation, and
    calling it a pass would hide a dropped parameter. Only evidence that the request was
    altered yields ``violated``.
    """

    index: int
    check: str
    title: str
    prompt: str = ''
    expected: str = ''
    observed: str = ''
    verdict: Literal['conformed', 'violated', 'inconclusive'] = 'inconclusive'
    severity: Literal['high', 'medium', 'low'] = 'medium'
    detail: str = ''
    latency_ms: float | None = None
    ttft_ms: float | None = None
    error: str | None = None


class CheckScore(Contract):
    conformed: int = 0
    violated: int = 0
    inconclusive: int = 0
    total: int = 0


class ContractSummary(Contract):
    total: int = 0
    conformed: int = 0
    violated: int = 0
    inconclusive: int = 0
    fidelity: float = 0
    by_check: dict[str, CheckScore] = Field(default_factory=dict)
    high_severity: int = 0


class LabStage(Contract):
    """One native observation, never a fabricated result."""

    index: int
    phase: Phase
    pressure: int
    repeat: int
    native_task_id: str
    status: Literal['running', 'passed', 'failed', 'unknown'] = 'running'
    requests: int = 0
    succeeded: int = 0
    seconds: float = 0
    success_percent: float = 0
    rpm: float = 0
    input_tpm: float | None = None
    output_tpm: float | None = None
    total_tpm: float | None = None
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    ttft_p99_ms: float | None = None
    tpot_p50_ms: float | None = None
    tpot_p95_ms: float | None = None
    tpot_p99_ms: float | None = None
    latency_avg_ms: float | None = None
    latency_p95_ms: float | None = None
    latency_p99_ms: float | None = None
    mean_input_tokens: float | None = None
    mean_output_tokens: float | None = None
    mean_concurrency: float | None = None
    """Little's Law: throughput x average latency. Reported, never assumed."""
    peak_concurrency: float | None = None
    """The concurrency the engine scheduled with; the *achieved* value is ``mean_concurrency``."""
    reason: str = '正在准备压测进程'


class LabFinding(Contract):
    """Conservative conclusion for a tested workload, not an account quota."""

    phase: Literal['rpm', 'tpm']
    value: float | None = None
    pressure: int | None = None
    status: Literal['pending', 'bounded', 'lower_bound', 'inconclusive'] = 'pending'
    verified: bool = False
    message: str = '等待测试'


class QualityItem(Contract):
    """One question, one answer, one deterministic verdict."""

    index: int
    category: str
    level: str = ''
    prompt: str
    media_count: int = 0
    expected: str
    answer: str = ''
    passed: bool = False
    ttft_ms: float | None = None
    latency_ms: float | None = None
    error: str | None = None


class CategoryScore(Contract):
    passed: int = 0
    total: int = 0


class QualitySummary(Contract):
    total: int = 0
    passed: int = 0
    accuracy: float = 0
    by_category: dict[str, CategoryScore] = Field(default_factory=dict)
    by_level: dict[str, CategoryScore] = Field(default_factory=dict)
    ttft_p50_ms: float | None = None
    latency_avg_ms: float | None = None
    output_tps: float | None = None


class LabRun(Contract):
    """Public and persistable run state; no secrets or request bodies."""

    run_id: str
    created_at: str
    kind: RunKind = 'capacity'
    settings: LabSettings | SpeedSettings | ParallelSettings | QualitySettings | ContractSettings
    fingerprint: str
    status: Literal['running', 'completed', 'stopping', 'stopped', 'error', 'interrupted'] = 'running'
    message: str = '实验已创建'
    reserved_requests: int = 0
    reserved_tokens: int = 0
    stages: list[LabStage] = Field(default_factory=list)
    findings: list[LabFinding] = Field(default_factory=list)
    items: list[QualityItem] = Field(default_factory=list)
    quality: QualitySummary | None = None
    contract: list[ContractItem] = Field(default_factory=list)
    contract_summary: ContractSummary | None = None
    warnings: list[str] = Field(default_factory=list)
    engine: str = 'relay_lab 自有控制器 / EvalScope perf 引擎'


class RunsResponse(Contract):
    """History including unfinished runs recovered after restart."""

    runs: list[LabRun]


class ModelsRequest(Contract):
    """Transient connection data for an explicit model-list request."""

    url: str = Field(min_length=8, max_length=2048)
    api_key: SecretStr | None = Field(default=None, exclude=True)

    @field_validator('url')
    @classmethod
    def validate_url(cls, value: str) -> str:
        """Reuse the experiment endpoint's URL and credential checks."""
        return ConnectionSettings.validate_url(value)


class ModelsResponse(Contract):
    """Available opaque IDs; discovery does not establish inference support."""

    models: list[str]
    endpoint: str
    truncated: bool = False


class DemoRequest(Contract):
    """Protocol for the zero-cost loopback demo."""

    protocol: Literal['openai', 'openai_responses'] = 'openai'


class Schedule(Contract):
    """A recurring experiment. The API key is memory-only, never persisted."""

    schedule_id: str
    label: str = Field(min_length=1, max_length=80)
    kind: Literal['capacity', 'speed', 'parallel', 'quality', 'contract']
    interval_minutes: int = Field(ge=5, le=1440, strict=True)
    created_at: str
    next_run_at: str
    enabled: bool = True
    needs_key: bool = False
    last_run_id: str | None = None
    last_status: str | None = None
    capacity: LabSettings | None = None
    speed: SpeedSettings | None = None
    parallel: ParallelSettings | None = None
    quality: QualitySettings | None = None
    contract: ContractSettings | None = None


class ScheduleRequest(Contract):
    """Create a schedule; the key is kept in memory only."""

    label: str = Field(min_length=1, max_length=80)
    kind: Literal['capacity', 'speed', 'parallel', 'quality', 'contract']
    interval_minutes: int = Field(ge=5, le=1440, strict=True)
    capacity: LabSettings | None = None
    speed: SpeedSettings | None = None
    parallel: ParallelSettings | None = None
    quality: QualitySettings | None = None
    contract: ContractSettings | None = None
    api_key: SecretStr | None = Field(default=None, exclude=True)

    @model_validator(mode='after')
    def validate_payload(self) -> 'ScheduleRequest':
        expected = {
            'capacity': self.capacity,
            'speed': self.speed,
            'parallel': self.parallel,
            'quality': self.quality,
            'contract': self.contract,
        }
        if expected[self.kind] is None:
            raise ValueError(f'{self.kind} 定时任务缺少对应的参数')
        return self


class SchedulesResponse(Contract):
    schedules: list[Schedule]


class ScheduleToggle(Contract):
    enabled: bool


class ScheduleKey(Contract):
    """Transient credential for a stored schedule; never persisted."""

    api_key: SecretStr | None = None
