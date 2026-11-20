"""Typed contracts for the relay capacity lab.

Every model here is ours. EvalScope is only used as a load-generation library,
so nothing in this module depends on its service layer or its web contracts.
"""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

Phase = Literal['rpm', 'tpm', 'speed', 'parallel', 'quality', 'contract', 'load']
RunKind = Literal['capacity', 'speed', 'parallel', 'quality', 'contract', 'load']

QUALITY_CATEGORIES_BY_PACK: dict[str, tuple[str, ...]] = {
    'standard': ('STEM', '人文', '社科', '其他'),
    'smoke': ('算术', '指令', 'JSON', '中文理解', '代码', '逻辑推理', '多模态'),
}
"""Category chips depend on the selected pack; unknown names must fail loudly."""

QUALITY_CATEGORIES: tuple[str, ...] = tuple(
    dict.fromkeys(category for categories in QUALITY_CATEGORIES_BY_PACK.values() for category in categories)
)

QUALITY_PACKS: tuple[str, ...] = ('standard', 'smoke')
"""standard = MMLU curated MCQ subset; smoke = original Chinese hand suite."""

DATASET_PRESETS: dict[str, dict[str, object]] = {
    'default': {
        'name': '内置短输入（约 35 token）',
        'input_tokens': None,
        'note': '默认口径：输入极短，瓶颈落在调度与排队，RPM 数字最接近"每分钟请求数"。',
    },
    'short-short': {
        'name': '短入短出（测 RPM 上限）',
        'input_tokens': 64,
        'note': '输入与输出都短，最适合测 RPM 上限；吞吐受解码拖累最小。',
    },
    'long-short': {
        'name': '长入短出（测预填充能力）',
        'input_tokens': 4096,
        'note': '长上下文 + 短回答，瓶颈落在预填充；此时 RPM 反映的是"每分钟能吃多少输入 token"。',
    },
    'short-long': {
        'name': '短入长出（测 TPM 上限）',
        'input_tokens': 64,
        'note': '短输入 + 长输出，瓶颈落在解码，最适合测 TPM 上限。',
    },
    'long-long': {
        'name': '长入长出（最重负载）',
        'input_tokens': 4096,
        'note': '输入输出都长，单请求成本最高；用于观察双重瓶颈叠加后的实际承载。',
    },
}
"""Named workloads. A relay's capacity depends more on token shape than on model name."""

PRESET_LABEL = {key: str(value['name']) for key, value in DATASET_PRESETS.items()}

CONTRACT_PROTOCOLS: tuple[str, ...] = ('openai', 'openai_responses', 'anthropic')
"""Wire protocols the contract suite can cover; callers may select a non-empty subset."""

CONTRACT_MODES: tuple[str, ...] = ('fast', 'full')
"""fast = core high-signal probes on the primary wire (no cache wait); full = complete KVV matrix."""

CONTRACT_CHECKS: tuple[str, ...] = (
    'system',
    'tools',
    'parallel_tools',
    'stream_tools',
    'tool_choice_auto',
    'tool_choice_required',
    'tool_choice_none',
    'tool_choice_named',
    'dynamic_tools',
    'dynamic_tools_both',
    'dynamic_tools_mutex',
    'dynamic_tools_role',
    'dynamic_tools_dup',
    'tool_choice_required_bare',
    'json_schema',
    'schema_errors',
    'tool_schema_reject',
    'max_tokens',
    'stop',
    'multi_turn',
    'thinking',
    'preserved_thinking',
    'thinking_roundtrip',
    'reasoning_effort',
    'reasoning_effort_invalid',
    'tool_roundtrip',
    'tool_followup',
    'errors',
    'auth_missing',
    'stream',
    'stream_usage',
    'cache',
    'cache_headers',
    'cache_headers_stream',
    'cache_headers_5m',
    'cache_headers_5m_stream',
    'cache_roundtrip',
    'cache_roundtrip_stream',
    'cache_roundtrip_5m',
    'cache_roundtrip_5m_stream',
    'cache_tools_prefix',
    'cache_tools_append',
    'cache_breakpoint',
    'anthropic_cache_invalid',
    'cache_options_echo',
    'vision_base64',
    'vision_url',
    'vision_content_string',
    'vision_video',
    'vision_svg_reject',
    'files_roundtrip',
    'fixed_sampling',
    'fixed_sampling_params',
    'thinking_disable',
    'thinking_disable_enable',
    'thinking_disable_template',
    'partial_mode',
    'partial_json_mix',
    'json_object',
    'logprobs',
    'routing',
    'response_shape',
    'usage',
    'cache_write_headers_audit',
    'error_fields_audit',
)
"""Relay-fidelity probes: does the gateway forward what it was sent?

These are protocol questions, not model questions. A relay that drops ``tools``, ignores
``stop``, buffers a stream into one burst or rewrites ``max_tokens`` still answers
plausibly, which is exactly why each probe is built to fail loudly when the request was
altered. An unknown name would silently probe nothing, so it is rejected.
"""

FAST_CONTRACT_CHECKS: tuple[str, ...] = (
    'system',
    'tools',
    'tool_choice_auto',
    'tool_choice_required',
    'tool_choice_none',
    'dynamic_tools',
    'dynamic_tools_mutex',
    'json_schema',
    'max_tokens',
    'stop',
    'multi_turn',
    'thinking',
    'thinking_roundtrip',
    'reasoning_effort',
    'tool_roundtrip',
    'errors',
    'auth_missing',
    'stream',
    'stream_usage',
    'cache',
    'cache_headers',
    'vision_base64',
    'fixed_sampling',
    'thinking_disable',
    'response_shape',
    'usage',
)
"""快速模式精简检查集：保留核心高信号防篡改与关键 KVV 契约项，跳过 15s 缓存等待与边缘矩阵。"""


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
    success_percent: float = Field(default=90, ge=90, le=100, allow_inf_nan=False)
    ttft_p99_ms: float = Field(default=3000, gt=0, le=120000, allow_inf_nan=False)
    tpot_p99_ms: float = Field(default=50, gt=0, le=10000, allow_inf_nan=False)
    capacity_ratio: float = Field(default=0.7, gt=0, le=1, allow_inf_nan=False)
    """Suggested carrying capacity = measured * this ratio (usually leave ~30% headroom)."""
    short_output_tokens: int = Field(default=32, ge=8, le=256, strict=True)
    long_output_tokens: int = Field(default=1024, ge=64, le=4096, strict=True)
    input_tokens: int | None = Field(default=None, ge=8, le=32000, strict=True)
    """Optional target input length; ``None`` keeps the small built-in prompt (~35 tokens).

    This changes *which bottleneck the ladder finds*, so it is not cosmetic:

    * a **short** input keeps the RPM ladder bound by scheduling and queueing, which is
      what makes an RPM figure mean "requests per minute" rather than "tokens per minute";
    * a **long** input moves the bottleneck into prefill, so the same ladder now measures
      how fast the site ingests context. That is a legitimate question, but a different
      one, and the two numbers are not comparable.

    The length is an estimate from a fixed words-per-token ratio, not a real tokenizer
    count: the goal is a *repeatable* workload of roughly this size, not an exact count.
    """
    prompt_reuse: bool = False
    """Send the same prompt every time, to measure throughput with a warm cache."""
    dataset_preset: str | None = None
    """Named workload from :data:`DATASET_PRESETS`; resolves to ``input_tokens``.

    Kept as a separate field so a report says *which* workload produced a number, not
    merely how long it was. It is applied after validation, so declaring a preset and a
    contradicting ``input_tokens`` is rejected rather than silently resolved.
    """
    seed: int = Field(default=42, ge=0, le=2147483647, strict=True)

    @model_validator(mode='after')
    def validate_bounds(self) -> 'LabSettings':
        if self.start_rpm > self.max_rpm:
            raise ValueError('起始 RPM 不能高于 RPM 安全上限')
        if self.dataset_preset is not None:
            preset = DATASET_PRESETS.get(self.dataset_preset)
            if preset is None:
                allowed = '、'.join(DATASET_PRESETS)
                raise ValueError(f'未知的数据集预设 {self.dataset_preset}；可选：{allowed}')
            declared = preset['input_tokens']
            # Both given and different: the caller is contradicting itself, and picking
            # one silently would make the report's stated workload a lie.
            if self.input_tokens is not None and declared is not None and self.input_tokens != declared:
                raise ValueError(
                    f'数据集预设「{self.dataset_preset}」的输入长度是 {declared} token，'
                    f'与显式指定的 {self.input_tokens} 冲突；请只保留一个'
                )
            if self.input_tokens is None:
                # ``model_fields_set`` distinguishes "absent" from an explicit null.
                self.input_tokens = declared  # type: ignore[assignment]
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
    """Built-in quality smoke suite with deterministic grading."""

    pack: Literal['standard', 'smoke'] = 'standard'
    categories: list[str] = Field(default_factory=list)
    per_category: int = Field(default=8, ge=1, le=20, strict=True)
    max_parallel: int = Field(default=1, ge=1, le=8, strict=True)
    request_timeout: int = Field(default=60, ge=2, le=300, strict=True)

    @field_validator('pack')
    @classmethod
    def validate_pack(cls, value: str) -> str:
        if value not in QUALITY_PACKS:
            allowed = '、'.join(QUALITY_PACKS)
            raise ValueError(f'未知的题包 {value}；可选：{allowed}')
        return value

    @field_validator('categories')
    @classmethod
    def validate_categories(cls, value: list[str]) -> list[str]:
        """An unknown name would silently select zero items and report 0%."""
        unknown = [name for name in value if name not in QUALITY_CATEGORIES]
        if unknown:
            allowed = '、'.join(QUALITY_CATEGORIES)
            raise ValueError(f'未知的题目分类 {unknown}；可选：{allowed}')
        return list(dict.fromkeys(value))

    @model_validator(mode='after')
    def categories_match_pack(self) -> 'QualitySettings':
        allowed = set(QUALITY_CATEGORIES_BY_PACK.get(self.pack, ()))
        mismatched = [name for name in self.categories if name not in allowed]
        if mismatched:
            names = '、'.join(QUALITY_CATEGORIES_BY_PACK.get(self.pack, ()))
            raise ValueError(f'分类 {mismatched} 不属于题包 {self.pack}；可选：{names}')
        return self


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


class LoadSettings(ConnectionSettings):
    """Closed-loop throughput: hold a fixed concurrency and count completions.

    Defaults match the console's “快速吞吐 / 恒定并发” profile: 100 slots for one
    minute. Raising ``concurrency_step`` / ``windows`` still allows a climb when
    the caller wants one.
    """

    concurrency_start: int = Field(default=100, ge=1, le=256, strict=True)
    concurrency_step: int = Field(default=0, ge=0, le=256, strict=True)
    window_seconds: int = Field(default=60, ge=5, le=60, strict=True)
    windows: int = Field(default=1, ge=1, le=12, strict=True)
    output_tokens: int = Field(default=64, ge=8, le=4096, strict=True)
    max_requests: int = Field(default=12000, ge=10, le=20000, strict=True)
    request_timeout: int = Field(default=150, ge=2, le=300, strict=True)
    success_percent: float = Field(default=90.0, ge=0, le=100, strict=True)
    """SLA lower bound: window passes only at or above this success rate."""
    circuit_success_percent: float = Field(default=80.0, ge=0, le=100, strict=True)
    """Circuit breaker: below this success rate the site is treated as overloaded."""
    max_consecutive_failures: int = Field(default=20, ge=0, le=10000, strict=True)
    """Circuit breaker: this many failures in a row marks overload; 0 disables."""
    avg_latency_ms: float = Field(default=150000, ge=0, le=600000, strict=True)
    """Circuit breaker: mean latency above this ends the scene; 0 disables."""
    ttft_p99_ms: float = Field(default=3000, gt=0, le=120000, allow_inf_nan=False)
    tpot_p99_ms: float = Field(default=50, gt=0, le=10000, allow_inf_nan=False)
    capacity_ratio: float = Field(default=0.7, gt=0, le=1, allow_inf_nan=False)
    prompt_reuse: bool = False
    """Send the same prompt every time, to measure throughput with a warm cache."""
    stream: bool = True
    """True sends SSE and scores first-byte TTFT. False waits for the whole JSON body."""
    stop_on_failure: bool = True
    """Stop the climb at the first window that fails the circuit or SLA."""
    saturation_ratio: float = Field(default=0.05, ge=0, le=1, strict=True)
    """Stop when throughput grows less than this share versus the previous window."""
    seed: int = Field(default=42, ge=0, le=2147483647, strict=True)

    @model_validator(mode='after')
    def validate_load_bounds(self) -> 'LoadSettings':
        # 快速吞吐底层使用 EvalScope perf 引擎，当前仅注册了 openai / openai_responses 插件。
        if self.protocol not in ('openai', 'openai_responses'):
            raise ValueError(f'快速吞吐暂不支持 {self.protocol} 协议，请选择 Chat Completions 或 Responses')
        peak_concurrency = self.concurrency_start + (self.windows - 1) * self.concurrency_step
        if peak_concurrency > 256:
            raise ValueError(f'爬坡后最高并发 {peak_concurrency} 超过上限 256，请调小起始并发、步长或窗口数')
        return self

    @property
    def duration(self) -> int:
        """Total measured time; the ramp is this many windows of window_seconds."""
        return self.window_seconds * self.windows


class LoadRequest(LoadSettings):
    """Throughput check plus the transient credential and consent flag."""

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

    mode: Literal['fast', 'full'] = 'full'
    """检查模式：fast（快速模式，核心项+主协议+无缓存等待）或 full（全量模式，完整 KVV+多协议）。"""
    checks: list[str] = Field(default_factory=list)
    # Empty → all CONTRACT_PROTOCOLS (full) or primary protocol (fast).
    protocols: list[str] = Field(default_factory=list)
    per_check: int = Field(default=1, ge=1, le=3, strict=True)
    max_parallel: int = Field(default=1, ge=1, le=8, strict=True)
    request_timeout: int = Field(default=60, ge=2, le=300, strict=True)
    cache_prefix_tokens: int = Field(default=2048, ge=512, le=8192, strict=True)
    cache_wait_seconds: int = Field(default=15, ge=15, le=300, strict=True)

    @field_validator('checks')
    @classmethod
    def validate_checks(cls, value: list[str]) -> list[str]:
        """Reject an unknown probe name instead of silently running fewer checks."""
        unknown = [name for name in value if name not in CONTRACT_CHECKS]
        if unknown:
            allowed = '、'.join(CONTRACT_CHECKS)
            raise ValueError(f'未知的契约检查 {unknown}；可选：{allowed}')
        return list(dict.fromkeys(value))

    @field_validator('protocols')
    @classmethod
    def validate_protocols(cls, value: list[str]) -> list[str]:
        """Keep contract wires inside the three supported protocols, in canonical order."""
        unknown = [name for name in value if name not in CONTRACT_PROTOCOLS]
        if unknown:
            allowed = '、'.join(CONTRACT_PROTOCOLS)
            raise ValueError(f'未知的协议 {unknown}；可选：{allowed}')
        selected = [name for name in CONTRACT_PROTOCOLS if name in value]
        return selected


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

    ``verdict`` is four-valued. ``inconclusive`` is missing evidence; ``observation`` is
    soft evidence (e.g. cache write=0 / named tool_choice 4xx) that must not dilute
    fidelity. Only evidence that the request was altered yields ``violated``.
    """

    index: int
    check: str
    title: str
    prompt: str = ''
    expected: str = ''
    observed: str = ''
    verdict: Literal['conformed', 'violated', 'inconclusive', 'observation'] = 'inconclusive'
    severity: Literal['high', 'medium', 'low'] = 'medium'
    detail: str = ''
    protocol: str = ''
    """Wire protocol used for this row (openai / openai_responses / anthropic)."""
    latency_ms: float | None = None
    ttft_ms: float | None = None
    error: str | None = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    """Per-round response headers aligned with each round in ``response_body``."""
    round_response_headers: list[dict[str, str]] = Field(default_factory=list)
    """Wire artifacts for the export report; never include the API key."""
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: str = ''
    response_body: str = ''
    """Expected response-header names that were absent or empty for this probe."""
    missing_response_headers: list[str] = Field(default_factory=list)
    requests: int = 0
    repeat: int = 1


class CheckScore(Contract):
    conformed: int = 0
    violated: int = 0
    inconclusive: int = 0
    observation: int = 0
    total: int = 0


class ContractSummary(Contract):
    total: int = 0
    conformed: int = 0
    violated: int = 0
    inconclusive: int = 0
    observation: int = 0
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
    # Only populated for finalized load windows with verified per-request logs.
    partial_results: bool = False
    window_rate_seconds: float | None = None
    wall_seconds: float | None = None
    token_source: Literal['reported', 'estimated', 'missing', 'unknown'] = 'unknown'
    window_requests: int | None = None
    window_succeeded: int | None = None
    window_success_percent: float | None = None
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
    latency_p50_ms: float | None = None
    latency_p90_ms: float | None = None
    latency_p95_ms: float | None = None
    failure_reasons: dict[str, int] = Field(default_factory=dict)
    """Why requests failed in this stage, counted from the per-request log."""
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
    status: Literal['pending', 'bounded', 'lower_bound', 'latency_floor', 'inconclusive'] = 'pending'
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
    source: str = ''
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


class LoadSummary(Contract):
    """Result of a throughput run: the best window, not a capacity boundary."""

    peak_concurrency: int | None = None
    peak_rpm: float | None = None
    suggested_rpm: float | None = None
    """peak_rpm * capacity_ratio; leave headroom before calling it carrying capacity."""
    success_total: int = 0
    request_total: int = 0
    windows: int = 0
    saturated: bool = False
    stop_reason: str | None = None


class AIAnalysis(Contract):
    summary_md: str
    model: str
    protocol: Literal['openai', 'openai_responses']
    created_at: str


class LabRun(Contract):
    """Public and persistable run state; no secrets or request bodies."""

    run_id: str
    created_at: str
    kind: RunKind = 'capacity'
    settings: LabSettings | SpeedSettings | ParallelSettings | QualitySettings | ContractSettings | LoadSettings
    fingerprint: str
    status: Literal['running', 'completed', 'stopping', 'stopped', 'error', 'interrupted'] = 'running'
    message: str = '实验已创建'
    reserved_requests: int = 0
    reserved_tokens: int = 0
    stages: list[LabStage] = Field(default_factory=list)
    findings: list[LabFinding] = Field(default_factory=list)
    items: list[QualityItem] = Field(default_factory=list)
    quality: QualitySummary | None = None
    load: LoadSummary | None = None
    contract: list[ContractItem] = Field(default_factory=list)
    contract_summary: ContractSummary | None = None
    ai_analysis: AIAnalysis | None = None
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
