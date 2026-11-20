"""Per-model-family protocol profiles for contract probes.

Relay fidelity checks must follow *that model's* official request/response contract.
Kimi and GLM both speak OpenAI Chat, but differ on token fields, thinking controls,
tool_choice, and whether ``system.tools`` exists at all. Matching by model name keeps
the suite honest without hard-coding one vendor into every probe.

Kimi K2 vs K3 share ``family='kimi'`` for cache/presets compatibility, but differ on
whether ``thinking.type=disabled`` is allowed (KVV / platform contract).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .config import CONTRACT_CHECKS

TokenField = Literal['max_completion_tokens', 'max_tokens']


@dataclass(frozen=True)
class ModelProfile:
    """How one model family expects Chat Completions / thinking / tools to look."""

    family: str
    label: str
    openai_token_field: TokenField = 'max_completion_tokens'
    reasoning_effort: str | None = None
    """Top-level ``reasoning_effort`` when the family documents it."""
    thinking: dict[str, Any] | None = None
    """Body ``thinking`` object (GLM). ``None`` means do not send."""
    error_style: str = 'openai_type'
    """Official error-body vocabulary: ``openai_type`` = error.type enum (OpenAI / Moonshot Kimi);
    ``code_message`` = error.code + message without a required type field (GLM)."""
    tool_choice: str | None = None
    """Value to send with tools probes; ``None`` omits the field (Kimi Postman style)."""
    clear_thinking: bool | None = None
    """When False, ask the site to retain prior ``reasoning_content`` (GLM)."""
    always_thinking: bool = False
    supports_dynamic_tools: bool = False
    supports_preserved_thinking: bool = False
    default_temperature: float = 1.0
    """Default Chat Completions temperature. Thinking-on Kimi/KVV uses 1.0; 0.6 is non-think."""
    allows_thinking_disable: bool = False
    """Whether ``thinking.type=disabled`` is a valid official path (K2.5/K2.6 yes; K3 no)."""
    default_checks: tuple[str, ...] = field(default_factory=lambda: CONTRACT_CHECKS)


def _checks(*names: str) -> tuple[str, ...]:
    allowed = set(CONTRACT_CHECKS)
    return tuple(name for name in CONTRACT_CHECKS if name in names and name in allowed)


# K2.5 / K2.6: thinking-on default temperature is 1.0; disabled thinking is allowed.
KIMI = ModelProfile(
    family='kimi',
    label='Kimi（Moonshot）',
    openai_token_field='max_completion_tokens',
    reasoning_effort='low',
    thinking=None,
    tool_choice=None,  # official / Postman: prompt-forced tools, no tool_choice
    clear_thinking=None,
    always_thinking=True,
    supports_dynamic_tools=True,  # system.tools without content
    supports_preserved_thinking=True,
    default_temperature=1.0,  # KVV verify_params: think → 1.0
    allows_thinking_disable=True,
    error_style='openai_type',
    default_checks=CONTRACT_CHECKS,
)

# K3 / K3.1 Pro / k2-thinking: always-on thinking; disable must be rejected or ignored.
KIMI_K3 = ModelProfile(
    family='kimi',
    label='Kimi K3（Moonshot）',
    openai_token_field='max_completion_tokens',
    reasoning_effort='low',
    thinking=None,
    tool_choice=None,
    clear_thinking=None,
    always_thinking=True,
    supports_dynamic_tools=True,
    supports_preserved_thinking=True,
    default_temperature=1.0,
    allows_thinking_disable=False,
    error_style='openai_type',
    default_checks=CONTRACT_CHECKS,
)

GLM = ModelProfile(
    family='glm',
    label='智谱 GLM',
    # docs.bigmodel.cn chat completions examples use max_tokens.
    openai_token_field='max_tokens',
    reasoning_effort='low',
    thinking={'type': 'enabled'},
    tool_choice='auto',  # official: tool_choice 默认且仅支持 auto
    clear_thinking=False,  # docs: clear_thinking false → 保留式思考
    always_thinking=True,
    supports_dynamic_tools=False,  # no official system.tools mechanism
    supports_preserved_thinking=True,
    default_temperature=1.0,
    allows_thinking_disable=False,
    error_style='code_message',
    default_checks=_checks(
        'system',
        'tools',
        'parallel_tools',
        'stream_tools',
        'tool_choice_auto',
        'tool_choice_required',
        'tool_choice_none',
        'tool_choice_named',
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
        'tool_roundtrip',
        'tool_followup',
        'errors',
        'auth_missing',
        'stream',
        'stream_usage',
        # Kimi-only Msh cache headers / prompt_cache_options are not in the GLM default pack.
        'vision_base64',
        'vision_url',
        'vision_content_string',
        'vision_video',
        'vision_svg_reject',
        'fixed_sampling',
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
    ),
)

GENERIC = ModelProfile(
    family='generic',
    label='通用 OpenAI 兼容',
    openai_token_field='max_tokens',
    reasoning_effort=None,
    thinking=None,
    tool_choice='auto',
    clear_thinking=None,
    always_thinking=False,
    supports_dynamic_tools=False,
    supports_preserved_thinking=False,
    default_temperature=1.0,
    allows_thinking_disable=False,
    default_checks=_checks(
        'system',
        'tools',
        'parallel_tools',
        'stream_tools',
        'tool_choice_auto',
        'tool_choice_required',
        'tool_choice_none',
        'tool_choice_named',
        'tool_choice_required_bare',
        'tool_roundtrip',
        'tool_followup',
        'json_schema',
        'schema_errors',
        'tool_schema_reject',
        'max_tokens',
        'stop',
        'multi_turn',
        'errors',
        'auth_missing',
        'stream',
        'stream_usage',
        # Kimi-only cache header suite omitted for generic OpenAI-compatible sites.
        'vision_base64',
        'vision_url',
        'vision_content_string',
        'vision_video',
        'vision_svg_reject',
        'partial_mode',
        'partial_json_mix',
        'json_object',
        'logprobs',
        'routing',
        'response_shape',
        'usage',
    ),
)


def resolve_profile(model: str) -> ModelProfile:
    """Pick the official contract profile from the model id."""
    name = (model or '').strip().lower()
    if not name:
        return GENERIC
    if 'kimi' in name or name.startswith('moonshot') or name.startswith('kimi-'):
        # K3 / K3.1 and locked-thinking K2 variants cannot disable thinking.
        if (
            'k3' in name
            or 'k2-thinking' in name
            or 'k2_thinking' in name
            or name.endswith('-thinking')
        ):
            return KIMI_K3
        return KIMI
    if 'glm' in name or 'chatglm' in name or name.startswith('zhipu'):
        return GLM
    return GENERIC


def apply_openai_profile(body: dict[str, Any], profile: ModelProfile) -> dict[str, Any]:
    """Rewrite an OpenAI Chat body to match the family's documented fields."""
    tokens = body.pop('max_completion_tokens', None)
    if tokens is None:
        tokens = body.get('max_tokens')
    if tokens is not None:
        body.pop('max_tokens', None)
        body.pop('max_completion_tokens', None)
        body[profile.openai_token_field] = tokens

    if profile.reasoning_effort is not None:
        body.setdefault('reasoning_effort', profile.reasoning_effort)
    else:
        body.pop('reasoning_effort', None)

    if profile.thinking is not None:
        thinking = dict(profile.thinking)
        if profile.clear_thinking is not None:
            thinking.setdefault('clear_thinking', profile.clear_thinking)
        body.setdefault('thinking', thinking)
    return body
