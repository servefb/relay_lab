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
example) returns ``None`` from the builder and the caller reports ``inconclusive``
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
    total_tokens: int | None = None


@dataclass
class Reply:
    """One completed call: text, tool calls, usage, timings, chunk evidence."""

    text: str = ''
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    ttft_ms: float | None = None
    latency_ms: float | None = None
    chunks: int = 0
    status: int | None = None
    error: str | None = None
    raw_text: str = ''
    finish_reason: str | None = None


class Unsupported(Exception):
    """This protocol cannot express the requested feature; the caller must not guess."""


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
    if path.endswith('/v1') and suffix.startswith('/v1/'):
        path = path[: -len('/v1')] + suffix
    else:
        path = path + suffix
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def endpoint(settings: Any, *, stream: bool = False) -> str:
    """Resolve the call URL for the declared protocol."""
    protocol = settings.protocol
    if protocol == 'openai':
        return _with_path(settings.url, '/v1/chat/completions', exact=('/chat/completions',))
    if protocol == 'openai_responses':
        return _with_path(settings.url, '/v1/responses', exact=('/responses',))
    if protocol == 'anthropic':
        return _with_path(settings.url, '/v1/messages', exact=('/messages',))
    if protocol == 'gemini':
        parts = urlsplit(settings.url)
        path = parts.path.rstrip('/')
        verb = 'streamGenerateContent' if stream else 'generateContent'
        head = path
        for marker in ('/v1beta/models', '/v1/models', '/models'):
            if marker in path:
                head = path.split(marker, 1)[0]
                break
        else:
            # A bare base or a custom proxy prefix: drop a trailing /v1beta or /v1 so the
            # version is not duplicated, then append the canonical Gemini path.
            for version in ('/v1beta', '/v1'):
                if head.endswith(version):
                    head = head[: -len(version)]
                    break
        return urlunsplit((parts.scheme, parts.netloc, f'{head}/v1beta/models/{settings.model}:{verb}', '', ''))
    raise Unsupported(f'未知协议 {protocol}')


def headers_for(settings: Any, api_key: str | None) -> dict[str, str]:
    """Auth shape differs per protocol; the key never appears in a URL we build."""
    headers = {'Content-Type': 'application/json'}
    if not api_key:
        return headers
    if settings.protocol == 'anthropic':
        headers['x-api-key'] = api_key
        headers['anthropic-version'] = getattr(settings, 'anthropic_version', None) or '2023-06-01'
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
            'temperature': 0,
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
            body['max_tokens'] = max_tokens
        if stop:
            body['stop'] = stop
        return body

    if protocol == 'openai_responses':
        # Responses takes instructions separately and turns history into input items.
        items = [
            {'role': message['role'], 'content': [{'type': 'input_text', 'text': message['content']}]}
            for message in conversation
        ]
        body = {'model': settings.model, 'input': items, 'temperature': 0, 'stream': stream}
        if system:
            body['instructions'] = system
        if max_tokens:
            body['max_output_tokens'] = max_tokens
        if tools:
            body['tools'] = [{'type': 'function', 'name': tool['name'], 'parameters': tool.get('parameters', {})} for tool in tools]
        if response_format:
            body['text'] = {'format': response_format}
        return body

    if protocol == 'anthropic':
        # max_tokens is mandatory for Messages; the caller's value or a safe default.
        body = {
            'model': settings.model,
            'max_tokens': max_tokens or 512,
            'messages': conversation,
            'temperature': 0,
            'stream': stream,
        }
        if system:
            body['system'] = system
        if tools:
            body['tools'] = [
                {'name': tool['name'], 'description': tool.get('description', ''), 'input_schema': tool.get('parameters', {})}
                for tool in tools
            ]
        if tool_choice == 'required':
            body['tool_choice'] = {'type': 'any'}
        elif isinstance(tool_choice, dict) and 'name' in tool_choice:
            body['tool_choice'] = {'type': 'tool', 'name': tool_choice['name']}
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
        generation: dict[str, Any] = {'temperature': 0}
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
        if stream:
            body['stream'] = True
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


def _parse_openai(payload: dict[str, Any], reply: Reply) -> None:
    choices = payload.get('choices') or []
    if choices:
        choice = choices[0] or {}
        # Streaming carries increments under "delta"; a non-streamed body puts the whole
        # turn under "message". Reading only one of them silently yields empty text.
        piece = choice.get('delta')
        if piece is None:
            piece = choice.get('message') or {}
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
            cached_tokens=details.get('cached_tokens'),
        )


def _parse_responses(payload: dict[str, Any], reply: Reply) -> None:
    event = payload.get('type')
    if event == 'response.output_text.delta':
        delta = payload.get('delta')
        if isinstance(delta, str):
            reply.text += delta
    elif event == 'response.output_item.done':
        item = payload.get('item') or {}
        if item.get('type') == 'function_call':
            _add_tool_call(reply, {'id': item.get('call_id'), 'function': item})
    elif event in ('response.completed', 'response.incomplete'):
        response = payload.get('response') or {}
        usage = response.get('usage') or {}
        details = usage.get('input_tokens_details') or {}
        _merge_usage(
            reply,
            prompt_tokens=usage.get('input_tokens'),
            completion_tokens=usage.get('output_tokens'),
            total_tokens=usage.get('total_tokens'),
            cached_tokens=details.get('cached_tokens'),
        )
        reply.finish_reason = response.get('status') or reply.finish_reason
    elif event in ('response.failed', 'error'):
        detail = payload.get('response', payload).get('error') or payload.get('error') or payload
        reply.error = 'HTTP 200 但事件异常：' + ' '.join(json.dumps(detail, ensure_ascii=False).split())[:180]


def _parse_anthropic(payload: dict[str, Any], reply: Reply) -> None:
    event = payload.get('type')
    if event == 'content_block_delta':
        delta = payload.get('delta') or {}
        if delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
            reply.text += delta['text']
    elif event == 'content_block_start':
        block = payload.get('content_block') or {}
        if block.get('type') == 'tool_use':
            _add_tool_call(reply, {'id': block.get('id'), 'function': block})
    elif event == 'message_start':
        usage = (payload.get('message') or {}).get('usage') or {}
        _merge_usage(
            reply,
            prompt_tokens=usage.get('input_tokens'),
            completion_tokens=usage.get('output_tokens'),
            cached_tokens=usage.get('cache_read_input_tokens'),
        )
    elif event == 'message_delta':
        usage = payload.get('usage') or {}
        _merge_usage(reply, completion_tokens=usage.get('output_tokens'))
        stop_reason = (payload.get('delta') or {}).get('stop_reason')
        if stop_reason:
            reply.finish_reason = stop_reason
    elif event == 'error':
        detail = payload.get('error') or payload
        reply.error = 'HTTP 200 但事件异常：' + ' '.join(json.dumps(detail, ensure_ascii=False).split())[:180]
    # A non-streamed Messages body arrives as one document.
    if event is None and isinstance(payload.get('content'), list):
        for block in payload['content']:
            if block.get('type') == 'text':
                reply.text += block.get('text') or ''
            elif block.get('type') == 'tool_use':
                _add_tool_call(reply, {'id': block.get('id'), 'function': block})
        usage = payload.get('usage') or {}
        _merge_usage(
            reply,
            prompt_tokens=usage.get('input_tokens'),
            completion_tokens=usage.get('output_tokens'),
            cached_tokens=usage.get('cache_read_input_tokens'),
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


def parse_sse_line(settings: Any, line: str, reply: Reply) -> bool:
    """Consume one SSE line; return False when the stream signalled completion.

    Only data-bearing lines count toward ``reply.chunks``. Blank separators, comments and
    keep-alives are framing, not streamed content: counting them would make a relay that
    buffered its whole answer into one burst look like a healthy stream.
    """
    if not line.startswith('data:'):
        return True
    chunk = line[5:].strip()
    if not chunk:
        return True
    if chunk == '[DONE]':
        return False
    try:
        payload = json.loads(chunk)
    except ValueError:
        return True
    if not isinstance(payload, dict):
        return True
    reply.chunks += 1
    parse_event(settings, payload, reply)
    return not reply.error


def json_schema_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """The strict structured-output envelope, in the requested protocol's words."""
    return {
        'type': 'json_schema',
        'json_schema': {'name': name, 'schema': schema, 'strict': True},
    }
