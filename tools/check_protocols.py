"""Verify the Anthropic and Gemini adapters build and parse correctly.

Wire-level assertions only: the URL, the auth header, the body shape and the parsing of
each protocol's own event vocabulary. No network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Running this file by path puts tools/ on sys.path, not the package root, so the
# project directory is added explicitly rather than requiring an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay_lab import protocols  # noqa: E402
from relay_lab.config import ContractSettings  # noqa: E402
from relay_lab.protocols import Reply  # noqa: E402


def settings(protocol: str, url: str = 'https://relay.test') -> ContractSettings:
    return ContractSettings(url=url, model='m-1', protocol=protocol, checks=['system'])


def main() -> None:
    problems = []

    # ---------- endpoints ----------
    cases = [
        ('anthropic', 'https://relay.test', 'https://relay.test/v1/messages'),
        ('anthropic', 'https://relay.test/v1/messages', 'https://relay.test/v1/messages'),
        ('openai', 'https://relay.test', 'https://relay.test/v1/chat/completions'),
        ('openai', 'https://relay.test/v1', 'https://relay.test/v1/chat/completions'),
        ('openai', 'https://relay.test/proxy/v1', 'https://relay.test/proxy/v1/chat/completions'),
        ('openai_responses', 'https://relay.test/v1', 'https://relay.test/v1/responses'),
        ('openai_responses', 'https://relay.test/v1/responses', 'https://relay.test/v1/responses'),
        ('gemini', 'https://relay.test', 'https://relay.test/v1beta/models/m-1:generateContent'),
        ('gemini', 'https://relay.test/v1beta', 'https://relay.test/v1beta/models/m-1:generateContent'),
        ('gemini', 'https://relay.test/proxy/v1beta', 'https://relay.test/proxy/v1beta/models/m-1:generateContent'),
    ]
    for protocol, url, expected in cases:
        got = protocols.endpoint(settings(protocol, url))
        print(f'{"OK  " if got == expected else "FAIL"} {protocol:17s} {got}')
        if got != expected:
            problems.append(f'{protocol} {url} -> {got} != {expected}')

    # Gemini streaming must opt into SSE explicitly: the bare verb answers one JSON
    # array, which iter_lines would discard line by line.
    stream_cases = [
        ('gemini', 'https://relay.test', 'https://relay.test/v1beta/models/m-1:streamGenerateContent?alt=sse'),
        ('gemini', 'https://relay.test/v1beta', 'https://relay.test/v1beta/models/m-1:streamGenerateContent?alt=sse'),
        ('openai', 'https://relay.test/v1', 'https://relay.test/v1/chat/completions'),
        ('anthropic', 'https://relay.test', 'https://relay.test/v1/messages'),
    ]
    for protocol, url, expected in stream_cases:
        got = protocols.endpoint(settings(protocol, url), stream=True)
        print(f'{"OK  " if got == expected else "FAIL"} {protocol:17s} stream {got}')
        if got != expected:
            problems.append(f'{protocol} stream {url} -> {got} != {expected}')

    # ---------- auth headers ----------
    anthropic = protocols.headers_for(settings('anthropic'), 'sk-secret')
    openai = protocols.headers_for(settings('openai'), 'sk-secret')
    if anthropic.get('x-api-key') != 'sk-secret' or 'Authorization' in anthropic:
        problems.append('anthropic must use x-api-key, not Authorization')
    if openai.get('Authorization') != 'Bearer sk-secret' or 'x-api-key' in openai:
        problems.append('openai must use Authorization Bearer')
    if 'anthropic-version' not in anthropic:
        problems.append('anthropic requires anthropic-version')
    for name, headers in (('anthropic', anthropic), ('openai', openai)):
        if any('sk-secret' in str(v) for k, v in headers.items() if k.lower() == 'content-type'):
            problems.append(f'{name}: key leaked into a non-auth header')
    print('headers anthropic:', {k: ('<key>' if k == 'x-api-key' else v) for k, v in anthropic.items()})
    print('headers openai   :', {k: ('<key>' if k == 'Authorization' else v) for k, v in openai.items()})

    # ---------- anthropic body ----------
    body = protocols.build_body(
        settings('anthropic'), 'hi', stream=False, system='S', max_tokens=64, stop=['8'], tools=[
            {'name': 'f', 'description': 'd', 'parameters': {'type': 'object', 'properties': {'a': {'type': 'string'}}}}
        ], tool_choice='required',
    )
    assert body['system'] == 'S'
    assert body['max_tokens'] == 64
    assert body['stop_sequences'] == ['8']
    assert body['tools'][0]['input_schema']['type'] == 'object'
    assert body['tool_choice'] == {'type': 'any'}
    assert body['messages'] == [{'role': 'user', 'content': 'hi'}]
    print('anthropic body keys:', sorted(body))

    # Anthropic requires max_tokens; a missing one must not silently send none.
    if 'max_tokens' not in protocols.build_body(settings('anthropic'), 'hi', stream=False):
        problems.append('anthropic body must always carry max_tokens')

    # Anthropic has no response_format: it must surface as Unsupported (probe ->
    # inconclusive), never as a silently weakened request.
    try:
        protocols.build_body(
            settings('anthropic'), 'hi', stream=False,
            response_format=protocols.json_schema_format('x', {'type': 'object', 'properties': {}}),
        )
        problems.append('anthropic response_format must raise Unsupported, not be dropped')
    except protocols.Unsupported:
        pass
    # Official tool_choice types: auto (omit), none, any, tool; anything else cannot be mapped.
    none_choice = protocols.build_body(settings('anthropic'), 'hi', stream=False, tool_choice='none')
    if none_choice.get('tool_choice') != {'type': 'none'}:
        problems.append(f'anthropic tool_choice=none mapping: {none_choice.get("tool_choice")!r}')
    try:
        protocols.build_body(settings('anthropic'), 'hi', stream=False, tool_choice='sometimes')
        problems.append('anthropic unknown tool_choice must raise Unsupported, not be dropped')
    except protocols.Unsupported:
        pass

    # ---------- gemini body ----------
    gem = protocols.build_body(
        settings('gemini'), 'hi', stream=False, system='S', max_tokens=32, stop=['9'],
        response_format=protocols.json_schema_format('x', {
            'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'],
        }),
        tools=[{'name': 'f', 'description': 'd', 'parameters': {'type': 'object', 'properties': {'a': {'type': 'string'}}}}],
    )
    assert gem['systemInstruction']['parts'][0]['text'] == 'S'
    assert gem['generationConfig']['maxOutputTokens'] == 32
    assert gem['generationConfig']['stopSequences'] == ['9']
    assert gem['generationConfig']['responseMimeType'] == 'application/json'
    # Gemini needs uppercase type enums, not JSON-Schema lowercase.
    assert gem['generationConfig']['responseSchema']['properties']['ok']['type'] == 'BOOLEAN'
    assert gem['tools'][0]['functionDeclarations'][0]['parameters']['properties']['a']['type'] == 'STRING'
    if 'stream' in gem:
        problems.append('gemini body must not carry the non-official stream field')
    print('gemini generationConfig:', gem['generationConfig'])

    # ---------- anthropic parsing (streamed) ----------
    reply = Reply()
    for event in [
        {'type': 'message_start', 'message': {'usage': {'input_tokens': 11, 'cache_read_input_tokens': 7}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking'}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': '先想'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text'}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': '你'}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': '好'}},
        {'type': 'content_block_stop', 'index': 1},
        {'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'tool_use', 'id': 't1', 'name': 'f', 'input': {}}},
        {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': '{"a"'}},
        {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': ':1}'}},
        {'type': 'content_block_stop', 'index': 2},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 5}},
    ]:
        protocols.parse_event(settings('anthropic'), event, reply)
    print('anthropic parsed:', reply.text, reply.reasoning, reply.usage, reply.finish_reason, reply.tool_calls)
    if reply.text != '你好':
        problems.append(f'anthropic text parse: {reply.text!r}')
    if reply.reasoning != '先想':
        problems.append(f'anthropic streamed thinking parse: {reply.reasoning!r}')
    if reply.usage.prompt_tokens != 11 or reply.usage.completion_tokens != 5 or reply.usage.cached_tokens != 7:
        problems.append(f'anthropic usage parse: {reply.usage}')
    if not reply.tool_calls or reply.tool_calls[0].get('name') != 'f':
        problems.append(f'anthropic streamed tool parse: {reply.tool_calls}')
    elif reply.tool_calls[0].get('arguments') != '{"a":1}':
        problems.append(f'anthropic streamed tool arguments not accumulated: {reply.tool_calls[0].get("arguments")!r}')

    # ---------- anthropic parsing (non-streamed document) ----------
    reply = Reply()
    protocols.parse_event(settings('anthropic'), {
        'content': [{'type': 'text', 'text': 'hi'}, {'type': 'tool_use', 'id': 't2', 'name': 'g', 'input': {'a': 1}}],
        'stop_reason': 'tool_use',
        'usage': {'input_tokens': 3, 'output_tokens': 4},
    }, reply)
    if reply.text != 'hi' or not reply.tool_calls or reply.tool_calls[0]['name'] != 'g':
        problems.append(f'anthropic non-stream parse: {reply.text!r} {reply.tool_calls}')
    elif json.loads(reply.tool_calls[0].get('arguments') or 'null') != {'a': 1}:
        problems.append(f'anthropic non-stream tool input dropped: {reply.tool_calls[0].get("arguments")!r}')
    print('anthropic non-stream:', reply.text, reply.tool_calls, reply.finish_reason)

    # ---------- gemini parsing ----------
    reply = Reply()
    protocols.parse_event(settings('gemini'), {
        'candidates': [{'content': {'parts': [{'text': 'ok'}, {'functionCall': {'name': 'f', 'args': {'a': 1}}}]}, 'finishReason': 'STOP'}],
        'usageMetadata': {'promptTokenCount': 8, 'candidatesTokenCount': 2, 'totalTokenCount': 10, 'cachedContentTokenCount': 6},
    }, reply)
    print('gemini parsed:', reply.text, reply.tool_calls, reply.usage, reply.finish_reason)
    if reply.text != 'ok' or reply.usage.cached_tokens != 6 or reply.usage.prompt_tokens != 8:
        problems.append(f'gemini parse: {reply.text!r} {reply.usage}')

    # A blocked prompt is reported in promptFeedback, not as an HTTP error.
    blocked = Reply()
    protocols.parse_event(settings('gemini'), {'promptFeedback': {'blockReason': 'SAFETY'}}, blocked)
    if not blocked.error:
        problems.append('gemini blockReason must surface as an error')
    print('gemini blocked ->', blocked.error)

    # A 200 whose JSON is Chat Completions shaped must not parse as silence: the
    # Messages path answered in the wrong wire format and that is the finding.
    wrong_shape = Reply()
    protocols.parse_event(settings('anthropic'), {'choices': [{'message': {'content': 'hi'}}]}, wrong_shape)
    if not wrong_shape.error:
        problems.append('anthropic parser must flag a chat-shaped body on the Messages path')
    print('anthropic chat-shaped body ->', wrong_shape.error)

    # ---------- responses parsing ----------
    reply = Reply()
    for event in [
        {'type': 'response.output_text.delta', 'delta': 'he'},
        {'type': 'response.output_text.delta', 'delta': 'y'},
        {'type': 'response.completed', 'response': {'status': 'completed', 'usage': {'input_tokens': 9, 'output_tokens': 2, 'input_tokens_details': {'cached_tokens': 4}}}},
    ]:
        protocols.parse_event(settings('openai_responses'), event, reply)
    if reply.text != 'hey' or reply.usage.cached_tokens != 4 or reply.usage.prompt_tokens != 9:
        problems.append(f'responses parse: {reply.text!r} {reply.usage}')
    print('responses parsed:', reply.text, reply.usage)

    # ---------- sse framing ----------
    reply = Reply()
    stream_settings = settings('openai')
    lines = ['', ': keep-alive', 'data: {"choices":[{"delta":{"content":"a"}}]}', '',
             'data: {"choices":[{"delta":{"content":"b"}}]}', 'data: [DONE]']
    for line in lines:
        if not protocols.parse_sse_line(stream_settings, line, reply):
            break
    if reply.chunks != 2 or reply.text != 'ab':
        problems.append(f'sse framing: chunks={reply.chunks} text={reply.text!r}')
    print('sse framing ->', reply.chunks, 'data chunks,', repr(reply.text), '(blank/comment lines excluded)')

    if problems:
        print('\nPROBLEMS:')
        for problem in problems:
            print(' -', problem)
        raise SystemExit(1)
    print('\nOK: all protocol adapters behave correctly.')


if __name__ == '__main__':
    main()
