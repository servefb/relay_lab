"""Protocol adapters: one request shape in, one normalized reply out.

Four wire protocols are spoken here, because a relay lab that only understands
``/v1/chat/completions`` cannot test the relays people actually buy:

* ``openai``          — Chat Completions
* ``openai_responses``— the Responses API (``input``, typed events)
* ``anthropic``       — Messages (``x-api-key``, ``system`` at top level, ``stop_sequences``)
* ``gemini``          — ``generateContent`` (model in the path, ``generationConfig``)

Everything above this module works with :class:`Reply` and :class:`Usage`, so a probe is
written once and runs against every protocol. Anything a protocol genuinely cannot
express (there is no ``stop_sequences`` equivalent in some Gemini deployments, for
example) raises ``Unsupported`` from the builder and the caller reports ``inconclusive``
instead of manufacturing a pass or a failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PROTOCOLS = ('openai', 'openai_responses', 'anthropic', 'gemini')


@dataclass
class Usage:
    """Token accounting, plus whatever cache counters the site chose to report."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_write_1h_tokens: int | None = None
    cache_write_5m_tokens: int | None = None
    thinking_tokens: int | None = None
    total_tokens: int | None = None


@dataclass
class Reply:
    """One completed call: text, tool calls, usage, timings, chunk evidence."""

    text: str = ''
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_block_slots: dict[int, int] = field(default_factory=dict)
    """Anthropic content-block index -> tool_calls slot, for streamed input_json_delta."""
    raw_body: str = ''
    """Beginning of the raw HTTP body, kept so an empty parse can still be explained."""
    response_text: str = ''
    """Larger raw response text for export reports; truncated, never includes secrets."""
    usage: Usage = field(default_factory=Usage)
    ttft_ms: float | None = None
    latency_ms: float | None = None
    chunks: int = 0
    """SSE events that actually add text or reasoning; metadata does not count."""
    sse_events: int = 0
    content_chunk_times_ms: list[float] = field(default_factory=list)
    """Elapsed request time at each content increment, excluding trailing metadata."""
    status: int | None = None
    error: str | None = None
    raw_text: str = ''
    finish_reason: str | None = None
    reasoning: str = ''
    headers: dict[str, str] = field(default_factory=dict)
    """Safe response headers kept for routing/cache diagnostics."""
    request_headers: dict[str, str] = field(default_factory=dict)
    """Outbound request headers with secrets redacted (for console / export)."""
    error_type: str | None = None
    error_code: str | None = None
    error_param: Any = None
    error_param_present: bool = False
    error_message: str | None = None
    stream_completed: bool = False
    """The protocol emitted its normal stream terminator."""
    payload: dict[str, Any] | None = None
    stream_final_response: dict[str, Any] | None = None
    """Responses 流式结束事件（response.completed / response.incomplete）携带的完整 response 对象。"""
    content_type: str = ''
    stream_errors: list[str] = field(default_factory=list)
    transport_error: bool = False
    responses_reasoning_delta_ids: set[str] = field(default_factory=set, repr=False)
    """记录 Responses 流式增量已接收过的 reasoning item_id，避免 output_item.done 重复拼接。"""
    responses_reasoning_delta_seen: bool = field(default=False, repr=False)


class Unsupported(Exception):
    """This protocol cannot express the requested feature; the caller must not guess."""


def is_moonshot_host(url: str) -> bool:
    """Moonshot / Kimi hosts use ``/anthropic/v1/messages`` and Bearer auth on that wire."""
    host = urlsplit(url).netloc.lower().split('@')[-1]
    return (
        'moonshot.cn' in host
        or host == 'kimi.com'
        or host.endswith('.kimi.com')
        or host == 'kimi.ai'
        or host.endswith('.kimi.ai')
        or 'moonshot' in host
    )


def site_root(url: str) -> tuple[str, str, str]:
    """Return ``(scheme, netloc, path)`` without endpoint / version / anthropic prefixes."""
    parts = urlsplit(url)
    path = parts.path.rstrip('/')
    for known in ('/chat/completions', '/responses', '/messages'):
        if path.endswith(known):
            path = path[: -len(known)]
            break
    if path.endswith(':generateContent') or path.endswith(':streamGenerateContent'):
        for marker in ('/v1beta/models', '/v1/models', '/models'):
            if marker in path:
                path = path.split(marker, 1)[0]
                break
    # Drop Moonshot's Anthropic mount, then a trailing /v1 so Chat/Responses/Anthropic
    # can each rebuild their own path from the same site root.
    if path.endswith('/anthropic/v1'):
        path = path[: -len('/anthropic/v1')]
    elif path.endswith('/anthropic'):
        path = path[: -len('/anthropic')]
    if path.endswith('/v1beta'):
        path = path[: -len('/v1beta')]
    elif path.endswith('/v1'):
        path = path[: -len('/v1')]
    return parts.scheme, parts.netloc, path


def _with_path(url: str, suffix: str, *, exact: tuple[str, ...] = ()) -> str:
    """Replace a known endpoint suffix, or add one without duplicating a version prefix.

    ``https://site/v1`` must become ``https://site/v1/responses``, not
    ``https://site/v1/v1/responses``: a doubled version segment 404s or, worse, lands on
    a SPA catch-all and returns HTML with a 200 (a failure mode this project has been
    bitten by before). A bare host still gets the full ``suffix``.
    """
    parts = urlsplit(url)
    path = parts.path.rstrip('/')
    for known in exact:
        if path.endswith(known):
            return urlunsplit((parts.scheme, parts.netloc, path, '', ''))
    scheme, netloc, root = site_root(url)
    return urlunsplit((scheme, netloc, f'{root}{suffix}', '', ''))


def endpoint(settings: Any, *, stream: bool = False) -> str:
    """Resolve the call URL for the declared protocol."""
    protocol = settings.protocol
    if protocol == 'openai':
        return _with_path(settings.url, '/v1/chat/completions', exact=('/chat/completions',))
    if protocol == 'openai_responses':
        return _with_path(settings.url, '/v1/responses', exact=('/responses',))
    if protocol == 'anthropic':
        # Official Moonshot Anthropic mount: https://api.moonshot.cn/anthropic/v1/messages
        suffix = '/anthropic/v1/messages' if is_moonshot_host(settings.url) else '/v1/messages'
        return _with_path(settings.url, suffix, exact=('/messages',))
    if protocol == 'gemini':
        scheme, netloc, root = site_root(settings.url)
        verb = 'streamGenerateContent' if stream else 'generateContent'
        # The bare streaming verb answers with one JSON array; SSE only arrives when
        # the request opts in with alt=sse.
        query = 'alt=sse' if stream else ''
        return urlunsplit((scheme, netloc, f'{root}/v1beta/models/{settings.model}:{verb}', query, ''))
    raise Unsupported(f'未知协议 {protocol}')


def headers_for(settings: Any, api_key: str | None) -> dict[str, str]:
    """Auth shape differs per protocol; the key never appears in a URL we build."""
    headers = {'Content-Type': 'application/json'}
    if not api_key:
        return headers
    if settings.protocol == 'anthropic':
        headers['anthropic-version'] = getattr(settings, 'anthropic_version', None) or '2023-06-01'
        # Moonshot's Anthropic-compatible curl uses Bearer; stock Anthropic uses x-api-key.
        if is_moonshot_host(getattr(settings, 'url', '') or ''):
            headers['Authorization'] = f'Bearer {api_key}'
        else:
            headers['x-api-key'] = api_key
    else:
        headers['Authorization'] = f'Bearer {api_key}'
    return headers


def build_body(
    settings: Any,
    prompt: str,
    *,
    stream: bool,
    system: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    response_format: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    temperature: float | int = 1,
    stop: list[str] | None = None,
    history: list[dict[str, str]] | None = None,
    images: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Translate one logical request into the selected protocol's body."""
    protocol = settings.protocol
    conversation = list(history or []) + [{'role': 'user', 'content': prompt}]

    if protocol == 'openai':
        body: dict[str, Any] = {
            'model': settings.model,
            'messages': ([{'role': 'system', 'content': system}] if system else []) + _openai_messages(conversation, images),
            'temperature': temperature,
            'stream': stream,
        }
        if stream:
            body['stream_options'] = {'include_usage': True}
        if tools:
            body['tools'] = [{'type': 'function', 'function': tool} for tool in tools]
        if tool_choice:
            body['tool_choice'] = tool_choice
        if response_format:
            body['response_format'] = response_format
        if max_tokens:
            # Reasoning / Kimi-class chat routes count thinking against the budget and
            # often reject legacy ``max_tokens``. ``max_completion_tokens`` is the field
            # their official curls use, and it covers both reasoning and final content.
            body['max_completion_tokens'] = max_tokens
        if stop:
            body['stop'] = stop
        return body

    if protocol == 'openai_responses':
        # Responses takes instructions separately and turns history into input items.
        items = [
            {'role': message['role'], 'content': [{'type': 'input_text', 'text': message['content']}]}
            for message in conversation
        ]
        if images and items:
            items[-1]['content'] += [
                {'type': 'input_image', 'image_url': image} for image in images
            ]
        body = {'model': settings.model, 'input': items, 'temperature': temperature, 'stream': stream}
        if system:
            body['instructions'] = system
        if max_tokens:
            body['max_output_tokens'] = max_tokens
        if tools:
            body['tools'] = [{'type': 'function', 'name': tool['name'], 'parameters': tool.get('parameters', {})} for tool in tools]
        if tool_choice:
            # Responses accepts the same required/auto/none strings as the current
            # OpenAI API.  Preserve a named choice when a caller supplies one.
            body['tool_choice'] = tool_choice
        if response_format:
            # Chat Completions wraps the schema in ``json_schema``; Responses puts
            # name/schema/strict directly under ``text.format``.
            if response_format.get('type') == 'json_schema':
                schema = response_format.get('json_schema') or {}
                body['text'] = {
                    'format': {
                        'type': 'json_schema',
                        'name': schema.get('name'),
                        'schema': schema.get('schema', {}),
                        'strict': schema.get('strict', True),
                    }
                }
            else:
                body['text'] = {'format': response_format}
        if stop:
            # Responses has no stop/stop_sequences field.  Silently dropping it would
            # turn the stop probe into a false reading of model behaviour.
            raise Unsupported('Responses API 没有 stop 参数')
        return body

    if protocol == 'anthropic':
        # max_tokens is mandatory for Messages; the caller's value or a safe default.
        messages = _anthropic_messages(conversation, images)
        body = {
            'model': settings.model,
            'max_tokens': max_tokens or 512,
            'messages': messages,
            'temperature': temperature,
            'stream': stream,
        }
        if system:
            body['system'] = system
        if response_format:
            raise Unsupported('Anthropic Messages 没有 response_format 参数')
        if tools:
            body['tools'] = [
                {'name': tool['name'], 'description': tool.get('description', ''), 'input_schema': tool.get('parameters', {})}
                for tool in tools
            ]
        # Official tool_choice types: auto (the default, omit), none, any, tool.
        if tool_choice == 'required':
            body['tool_choice'] = {'type': 'any'}
        elif isinstance(tool_choice, dict) and 'name' in tool_choice:
            body['tool_choice'] = {'type': 'tool', 'name': tool_choice['name']}
        elif tool_choice == 'none':
            body['tool_choice'] = {'type': 'none'}
        elif tool_choice not in (None, 'auto'):
            raise Unsupported(f'Anthropic Messages 无法表达 tool_choice={tool_choice!r}')
        if stop:
            body['stop_sequences'] = stop
        return body

    if protocol == 'gemini':
        contents = [
            {'role': 'model' if message['role'] == 'assistant' else 'user', 'parts': [{'text': message['content']}]}
            for message in conversation
        ]
        if images:
            contents[-1]['parts'] += [
                {'inline_data': {'mime_type': 'image/png', 'data': image.split(',', 1)[-1]}} for image in images
            ]
        generation: dict[str, Any] = {'temperature': temperature}
        if max_tokens:
            generation['maxOutputTokens'] = max_tokens
        if stop:
            generation['stopSequences'] = stop
        if response_format and response_format.get('type') == 'json_schema':
            schema = response_format.get('json_schema', {}).get('schema')
            if schema:
                # Gemini has no "json_schema" wrapper: the schema itself is the contract.
                generation['responseMimeType'] = 'application/json'
                generation['responseSchema'] = _gemini_schema(schema)
        body = {'contents': contents, 'generationConfig': generation}
        if system:
            body['systemInstruction'] = {'parts': [{'text': system}]}
        if tools:
            body['tools'] = [{'functionDeclarations': [_gemini_tool(tool) for tool in tools]}]
        return body

    raise Unsupported(f'未知协议 {protocol}')


def _openai_messages(conversation: list[dict[str, str]], images: tuple[str, ...]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [dict(message) for message in conversation]
    if images and messages:
        last = messages[-1]
        last['content'] = [{'type': 'text', 'text': last['content']}] + [
            {'type': 'image_url', 'image_url': {'url': image}} for image in images
        ]
    return messages


def _anthropic_messages(conversation: list[dict[str, str]], images: tuple[str, ...]) -> list[dict[str, Any]]:
    """Messages API image parts use ``source.base64``; keep text-only turns as strings."""
    messages: list[dict[str, Any]] = [dict(message) for message in conversation]
    if not images or not messages:
        return messages
    last = messages[-1]
    parts: list[dict[str, Any]] = []
    for image in images:
        media_type = 'image/png'
        data = image
        if image.startswith('data:'):
            header, _, payload = image.partition(',')
            media_type = header[5:].split(';', 1)[0] or 'image/png'
            data = payload
        parts.append({
            'type': 'image',
            'source': {'type': 'base64', 'media_type': media_type, 'data': data},
        })
    text = last.get('content')
    if isinstance(text, str) and text:
        parts.append({'type': 'text', 'text': text})
    last['content'] = parts
    return messages


def _gemini_tool(tool: dict[str, Any]) -> dict[str, Any]:
    parameters = tool.get('parameters') or {'type': 'object', 'properties': {}}
    return {
        'name': tool['name'],
        'description': tool.get('description', ''),
        'parameters': _gemini_schema(parameters),
    }


_GEMINI_TYPES = {'string': 'STRING', 'number': 'NUMBER', 'integer': 'INTEGER', 'boolean': 'BOOLEAN', 'array': 'ARRAY', 'object': 'OBJECT'}


def _gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite lowercase JSON-Schema types into Gemini's enum casing."""
    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key == 'type' and isinstance(value, str):
            result['type'] = _GEMINI_TYPES.get(value, value.upper())
        elif key == 'properties' and isinstance(value, dict):
            result['properties'] = {name: _gemini_schema(spec) for name, spec in value.items()}
        elif key == 'items' and isinstance(value, dict):
            result['items'] = _gemini_schema(value)
        else:
            result[key] = value
    return result


def parse_event(settings: Any, payload: dict[str, Any], reply: Reply) -> None:
    """Fold one decoded SSE object (or one JSON body) into *reply*."""
    if payload.get('error'):
        reply.error = '响应包含 error：' + json.dumps(payload['error'], ensure_ascii=False)[:180]
        return
    protocol = settings.protocol
    if protocol == 'openai':
        _parse_openai(payload, reply)
    elif protocol == 'openai_responses':
        _parse_responses(payload, reply)
    elif protocol == 'anthropic':
        _parse_anthropic(payload, reply)
    elif protocol == 'gemini':
        _parse_gemini(payload, reply)


def _merge_usage(reply: Reply, **values: Any) -> None:
    usage = reply.usage
    for name, value in values.items():
        if value is not None:
            setattr(usage, name, value)


def _cache_usage(usage: dict[str, Any], details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize the cache fields used by the Postman Chat and Messages checks."""
    if details is None:
        details = usage.get('prompt_tokens_details')
    details = details if isinstance(details, dict) else {}
    creation = usage.get('cache_creation')
    creation = creation if isinstance(creation, dict) else {}
    read = usage.get('cache_read_input_tokens')
    write = usage.get('cache_creation_input_tokens')
    return {
        'cached_tokens': read if read is not None else details.get('cached_tokens'),
        'cache_write_tokens': write if write is not None else details.get('cache_write_tokens'),
        'cache_write_1h_tokens': creation.get('ephemeral_1h_input_tokens'),
        'cache_write_5m_tokens': creation.get('ephemeral_5m_input_tokens'),
    }


def _parse_openai(payload: dict[str, Any], reply: Reply) -> None:
    if payload.get('error'):
        reply.error = 'HTTP 200 但响应包含 error：' + json.dumps(payload['error'], ensure_ascii=False)[:180]
        return
    choices = payload.get('choices') or []
    if choices:
        choice = choices[0] or {}
        # Streaming carries increments under "delta"; a non-streamed body puts the whole
        # turn under "message". Reading only one of them silently yields empty text.
        piece = choice.get('delta')
        if piece is None:
            piece = choice.get('message') or {}
        reasoning = piece.get('reasoning_content')
        if isinstance(reasoning, str):
            reply.reasoning += reasoning
        text = piece.get('content')
        if isinstance(text, str) and text:
            reply.text += text
        elif isinstance(text, list):
            # Some relays echo content-parts even for text turns.
            for part in text:
                if isinstance(part, dict) and isinstance(part.get('text'), str):
                    reply.text += part['text']
        for call in piece.get('tool_calls') or []:
            _add_tool_call(reply, call)
        if choice.get('finish_reason'):
            reply.finish_reason = choice['finish_reason']
    usage = payload.get('usage') or {}
    if usage:
        details = usage.get('prompt_tokens_details') or {}
        _merge_usage(
            reply,
            prompt_tokens=usage.get('prompt_tokens'),
            completion_tokens=usage.get('completion_tokens'),
            total_tokens=usage.get('total_tokens'),
            **_cache_usage(usage, details),
        )


def _parse_responses(payload: dict[str, Any], reply: Reply) -> None:
    event = payload.get('type')
    if event == 'response.output_text.delta':
        delta = payload.get('delta')
        if isinstance(delta, str):
            reply.text += delta
    elif event in (
        'response.reasoning_summary_text.delta',
        'response.reasoning_text.delta',
        'response.reasoning.delta',
    ):
        delta = payload.get('delta')
        added = ''
        if isinstance(delta, str):
            added = delta
        elif isinstance(delta, dict):
            text = delta.get('text')
            if isinstance(text, str):
                added = text
        if added:
            reply.reasoning += added
            reply.responses_reasoning_delta_seen = True
            item_id = payload.get('item_id')
            if isinstance(item_id, str) and item_id:
                reply.responses_reasoning_delta_ids.add(item_id)
    elif event == 'response.output_item.done':
        item = payload.get('item') or {}
        if item.get('type') == 'function_call':
            _add_tool_call(reply, {'id': item.get('call_id'), 'function': item})
        elif item.get('type') == 'reasoning':
            item_id = item.get('id')
            # 若该 reasoning 项已通过 *.delta 增量流式累计过，则不在 done 事件重复拼接全文。
            already_streamed = (
                (isinstance(item_id, str) and item_id in reply.responses_reasoning_delta_ids)
                or (reply.responses_reasoning_delta_seen and not reply.responses_reasoning_delta_ids)
            )
            if not already_streamed:
                for part in item.get('summary') or item.get('content') or []:
                    if isinstance(part, dict) and isinstance(part.get('text'), str):
                        reply.reasoning += part['text']
    elif event in ('response.completed', 'response.incomplete'):
        reply.stream_completed = True
        response = payload.get('response') or {}
        if isinstance(response, dict):
            reply.stream_final_response = response
        usage = response.get('usage') or {}
        details = usage.get('input_tokens_details') or {}
        _merge_usage(
            reply,
            prompt_tokens=usage.get('input_tokens'),
            completion_tokens=usage.get('output_tokens'),
            total_tokens=usage.get('total_tokens'),
            **_cache_usage(usage, details),
        )
        if event == 'response.incomplete' or response.get('status') == 'incomplete':
            reason = (response.get('incomplete_details') or {}).get('reason')
            reply.finish_reason = 'length' if reason == 'max_output_tokens' else (reason or response.get('status'))
        else:
            reply.finish_reason = response.get('status') or reply.finish_reason
    elif event in ('response.failed', 'error'):
        detail = payload.get('response', payload).get('error') or payload.get('error') or payload
        reply.error = 'HTTP 200 但事件异常：' + ' '.join(json.dumps(detail, ensure_ascii=False).split())[:180]

    # A non-streamed Responses response is one object with an ``output`` array,
    # rather than a typed event.  Without this branch every successful non-stream
    # probe looked like an empty reply and was reported as inconclusive.
    if event is None and ('output' in payload or payload.get('object') == 'response'):
        output_text = payload.get('output_text')
        found_output_text = False
        for item in payload.get('output') or []:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'function_call':
                _add_tool_call(reply, {'id': item.get('call_id'), 'function': item})
            if item.get('type') == 'reasoning':
                # OpenAI Responses: reasoning items carry summary_text / text parts.
                for part in item.get('summary') or item.get('content') or []:
                    if not isinstance(part, dict):
                        continue
                    text = part.get('text')
                    if isinstance(text, str):
                        reply.reasoning += text
                continue
            for part in item.get('content') or []:
                if not isinstance(part, dict):
                    continue
                if part.get('type') in ('output_text', 'text'):
                    text = part.get('text')
                    if isinstance(text, str):
                        reply.text += text
                        found_output_text = True
                elif part.get('type') in ('reasoning_text', 'summary_text') and isinstance(part.get('text'), str):
                    reply.reasoning += part['text']
        if isinstance(output_text, str) and not found_output_text:
            reply.text += output_text
        usage = payload.get('usage') or {}
        details = usage.get('input_tokens_details') or {}
        _merge_usage(
            reply,
            prompt_tokens=usage.get('input_tokens'),
            completion_tokens=usage.get('output_tokens'),
            total_tokens=usage.get('total_tokens'),
            **_cache_usage(usage, details),
        )
        if payload.get('status') == 'incomplete':
            reason = (payload.get('incomplete_details') or {}).get('reason')
            reply.finish_reason = 'length' if reason == 'max_output_tokens' else reason
        elif payload.get('status'):
            reply.finish_reason = payload['status']


def _parse_anthropic(payload: dict[str, Any], reply: Reply) -> None:
    event = payload.get('type')
    if event == 'content_block_delta':
        delta = payload.get('delta') or {}
        kind = delta.get('type')
        if kind == 'text_delta' and isinstance(delta.get('text'), str):
            reply.text += delta['text']
        elif kind == 'thinking_delta' and isinstance(delta.get('thinking'), str):
            reply.reasoning += delta['thinking']
        elif kind == 'input_json_delta' and isinstance(delta.get('partial_json'), str):
            # Tool input streams as partial-JSON fragments; drop one and the
            # accumulated arguments stop parsing.
            slot = reply.tool_block_slots.get(payload.get('index'))
            if slot is not None:
                reply.tool_calls[slot]['arguments'] += delta['partial_json']
    elif event == 'content_block_start':
        block = payload.get('content_block') or {}
        if block.get('type') == 'tool_use':
            _add_tool_call(reply, {'id': block.get('id'), 'function': block})
            reply.tool_block_slots[payload.get('index')] = len(reply.tool_calls) - 1
    elif event == 'message_start':
        usage = (payload.get('message') or {}).get('usage') or {}
        out_details = usage.get('output_tokens_details')
        thinking_tokens = out_details.get('thinking_tokens') if isinstance(out_details, dict) else None
        _merge_usage(
            reply,
            prompt_tokens=usage.get('input_tokens'),
            completion_tokens=usage.get('output_tokens'),
            thinking_tokens=thinking_tokens,
            **_cache_usage(usage),
        )
    elif event == 'message_delta':
        usage = payload.get('usage') or {}
        delta_input = usage.get('input_tokens')
        # 部分网关（如 new-api rc.49）在 message_start 给 input_tokens=0，在 message_delta 才补回真实值；
        # 避免用 message_delta 的 0 覆盖 message_start 已有的正数输入计数。
        prompt_tokens = (
            delta_input
            if delta_input is not None and (reply.usage.prompt_tokens in (None, 0) or delta_input > 0)
            else None
        )
        out_details = usage.get('output_tokens_details')
        thinking_tokens = out_details.get('thinking_tokens') if isinstance(out_details, dict) else None
        _merge_usage(
            reply,
            prompt_tokens=prompt_tokens,
            completion_tokens=usage.get('output_tokens'),
            thinking_tokens=thinking_tokens,
            **_cache_usage(usage),
        )
        stop_reason = (payload.get('delta') or {}).get('stop_reason')
        if stop_reason:
            reply.finish_reason = stop_reason
    elif event == 'message_stop':
        reply.stream_completed = True
    elif event == 'error':
        detail = payload.get('error') or payload
        reply.error = 'HTTP 200 但事件异常：' + ' '.join(json.dumps(detail, ensure_ascii=False).split())[:180]
    # A non-streamed Messages body arrives as one document.
    if event is None and isinstance(payload.get('choices'), list) and 'content' not in payload:
        # A 200 whose JSON is Chat Completions shaped means the site accepted the
        # Messages request but answered in another wire format; parsing it as silence
        # would report "empty reply" instead of the actual contract break.
        reply.error = '站点在 Anthropic Messages 路径返回了 Chat Completions 形状（含 choices），未按 Messages 契约响应'
    if event in {None, 'message'} and isinstance(payload.get('content'), list):
        for block in payload['content']:
            if block.get('type') == 'text':
                reply.text += block.get('text') or ''
            elif block.get('type') == 'thinking':
                thinking = block.get('thinking') or block.get('text') or ''
                if isinstance(thinking, str):
                    reply.reasoning += thinking
            elif block.get('type') == 'tool_use':
                _add_tool_call(
                    reply,
                    {'id': block.get('id'), 'function': {'name': block.get('name'), 'arguments': block.get('input')}},
                )
    # 思考在预算内耗尽或网关裁剪 content 时，非流式 message 响应可能省略 content 数组；
    # 只要是 Messages 响应结构（而非 Chat choices 串型或 error 对象），仍需提取 usage 与 stop_reason。
    if event in {None, 'message'} and not isinstance(payload.get('choices'), list) and (
        isinstance(payload.get('content'), list)
        or payload.get('type') == 'message'
        or payload.get('stop_reason') is not None
    ):
        usage = payload.get('usage') or {}
        if isinstance(usage, dict) and usage:
            out_details = usage.get('output_tokens_details')
            thinking_tokens = out_details.get('thinking_tokens') if isinstance(out_details, dict) else None
            _merge_usage(
                reply,
                prompt_tokens=usage.get('input_tokens'),
                completion_tokens=usage.get('output_tokens'),
                thinking_tokens=thinking_tokens,
                **_cache_usage(usage),
            )
        if payload.get('stop_reason'):
            reply.finish_reason = payload['stop_reason']


def _parse_gemini(payload: dict[str, Any], reply: Reply) -> None:
    candidates = payload.get('candidates') or []
    if candidates:
        candidate = candidates[0] or {}
        for part in (candidate.get('content') or {}).get('parts') or []:
            if isinstance(part.get('text'), str):
                reply.text += part['text']
            if part.get('functionCall'):
                _add_tool_call(reply, {'function': {'name': part['functionCall'].get('name'), 'arguments': part['functionCall'].get('args')}})
        if candidate.get('finishReason'):
            reply.finish_reason = candidate['finishReason']
            reply.stream_completed = True
    usage = payload.get('usageMetadata') or {}
    if usage:
        _merge_usage(
            reply,
            prompt_tokens=usage.get('promptTokenCount'),
            completion_tokens=usage.get('candidatesTokenCount'),
            total_tokens=usage.get('totalTokenCount'),
            cached_tokens=usage.get('cachedContentTokenCount'),
        )
    # Gemini reports a blocked prompt in promptFeedback rather than an HTTP error.
    feedback = payload.get('promptFeedback') or {}
    if feedback.get('blockReason') and not reply.text:
        reply.error = f'提示被拦截：{feedback["blockReason"]}'


def _add_tool_call(reply: Reply, call: dict[str, Any]) -> None:
    """Accumulate streamed tool-call fragments into one entry per index."""
    index = call.get('index', len(reply.tool_calls))
    function = call.get('function') or {}
    while len(reply.tool_calls) <= index:
        reply.tool_calls.append({'name': '', 'arguments': ''})
    entry = reply.tool_calls[index]
    if function.get('name'):
        entry['name'] = function['name']
    arguments = function.get('arguments')
    if isinstance(arguments, str):
        entry['arguments'] += arguments
    elif isinstance(arguments, dict):
        entry['arguments'] = json.dumps(arguments, ensure_ascii=False)
    if call.get('id'):
        entry['id'] = call['id']


def parse_sse_line(settings: Any, line: str, reply: Reply, *, elapsed_ms: float | None = None) -> bool:
    """Consume one SSE line; return False when the stream signalled completion.

    Only events adding text or reasoning count toward ``reply.chunks``. Role, usage,
    finish and framing events cannot demonstrate that generated content was streamed.
    """
    if not line.startswith('data:'):
        return True
    chunk = line[5:].strip()
    if not chunk:
        return True
    if chunk == '[DONE]':
        if settings.protocol == 'openai':
            reply.stream_completed = True
        return False
    try:
        def unique_pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('duplicate JSON key')
                result[key] = value
            return result
        def invalid_constant(value):
            raise ValueError(f'invalid JSON constant {value}')
        payload = json.loads(chunk, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    except ValueError:
        reply.stream_errors.append('SSE data 不是有效 JSON')
        return True
    if not isinstance(payload, dict):
        reply.stream_errors.append('SSE data 不是 JSON 对象')
        return True
    reply.sse_events += 1
    before = len(reply.text) + len(reply.reasoning)
    before_tools = len(reply.tool_calls)
    before_tool_bytes = len(json.dumps(reply.tool_calls, ensure_ascii=False))
    completed = reply.stream_completed
    if payload.get('error'):
        reply.error = 'HTTP 200 但 SSE 包含 error：' + json.dumps(payload['error'], ensure_ascii=False)[:180]
    if settings.protocol == 'openai_responses' and payload.get('type') in ('response.completed', 'response.incomplete'):
        response = payload.get('response')
        status = response.get('status') if isinstance(response, dict) else None
        if not isinstance(response, dict) or status not in ('completed', 'incomplete') or response.get('error'):
            reply.stream_errors.append('Responses 结束事件缺少有效终态（completed/incomplete）或包含错误')
    if settings.protocol == 'openai_responses' and not str(payload.get('type', '')).startswith('response.') and payload.get('type') != 'error':
        reply.stream_errors.append('Responses SSE 缺少有效事件类型')
    if settings.protocol == 'anthropic' and payload.get('type') not in (
        'message_start', 'message_delta', 'message_stop', 'content_block_start',
        'content_block_delta', 'content_block_stop', 'ping', 'error',
    ):
        reply.stream_errors.append('Messages SSE 缺少有效事件类型')
    if settings.protocol == 'gemini' and not any(key in payload for key in ('candidates', 'usageMetadata', 'promptFeedback', 'error')):
        reply.stream_errors.append('Gemini SSE 缺少有效响应字段')
    try:
        parse_event(settings, payload, reply)
    except (AttributeError, TypeError, KeyError, IndexError, ValueError):
        reply.stream_errors.append('SSE 事件字段结构不符合所选协议')
        return True
    if settings.protocol == 'openai' and not isinstance(payload.get('choices'), list) and not payload.get('error'):
        reply.stream_errors.append('Chat SSE 缺少 choices 数组')
    # Count tool-call name/argument deltas as streamed content (KVV stream tool calls).
    tool_grew = (
        len(reply.tool_calls) > before_tools
        or len(json.dumps(reply.tool_calls, ensure_ascii=False)) > before_tool_bytes
    )
    if len(reply.text) + len(reply.reasoning) > before or tool_grew:
        if completed:
            reply.stream_errors.append('正常结束事件后仍收到内容')
        reply.chunks += 1
        if elapsed_ms is not None:
            reply.content_chunk_times_ms.append(elapsed_ms)
    return not reply.error


def json_schema_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """The strict structured-output envelope, in the requested protocol's words."""
    return {
        'type': 'json_schema',
        'json_schema': {'name': name, 'schema': schema, 'strict': True},
    }
