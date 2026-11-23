"""Contract regressions against native HTTP fixtures, with no external requests."""

from __future__ import annotations

import copy
import json
import re
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from relay_lab import contract, protocols
from relay_lab.config import CONTRACT_CHECKS, FAST_CONTRACT_CHECKS, ContractSettings
from relay_lab.protocol_validation import shape_errors, usage_errors

PROTOCOLS = ['openai', 'openai_responses', 'anthropic', 'gemini']


def settings(protocol='openai', **kwargs):
    return ContractSettings(url='https://fixture.test', model='kimi-k3.1-pro', protocol=protocol, **kwargs)


def answer(protocol, text='OK', tool=False):
    args = {'ticket_id': 'T-1024'}
    if protocol == 'openai':
        message = {'role': 'assistant', 'content': None if tool else text}
        if tool:
            message['reasoning_content'] = 'Need a ticket lookup'
            message['tool_calls'] = [{'id': 'call_1', 'type': 'function', 'function': {
                'name': 'lookup_ticket', 'arguments': json.dumps(args),
            }}]
        return {'id': 'chat_1', 'object': 'chat.completion', 'model': 'fixture', 'created': 1,
                'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls' if tool else 'stop'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12,
                          'prompt_tokens_details': {'cache_write_tokens': 0}}}
    if protocol == 'openai_responses':
        output = [{'id': 'fc_1', 'type': 'function_call', 'call_id': 'call_1', 'name': 'lookup_ticket', 'arguments': json.dumps(args)}] if tool else [
            {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]},
        ]
        if tool:
            output.insert(0, {'id': 'rs_1', 'type': 'reasoning', 'summary': [], 'encrypted_content': 'opaque'})
        return {'id': 'resp_1', 'object': 'response', 'model': 'fixture', 'status': 'completed',
                'store': False, 'background': False, 'previous_response_id': None, 'conversation': None,
                'output': output,
                'usage': {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12,
                          'input_tokens_details': {'cache_write_tokens': 0}}}
    if protocol == 'anthropic':
        content = [{'type': 'tool_use', 'id': 'call_1', 'name': 'lookup_ticket', 'input': args}] if tool else [{'type': 'text', 'text': text}]
        if tool:
            content.insert(0, {'type': 'thinking', 'thinking': 'Need a ticket lookup', 'signature': 'opaque'})
        return {'id': 'msg_1', 'model': 'fixture', 'type': 'message', 'role': 'assistant', 'content': content,
                'stop_reason': 'tool_use' if tool else 'end_turn',
                'usage': {'input_tokens': 10, 'output_tokens': 2, 'output_tokens_details': {'thinking_tokens': 0}}}
    parts = [{'functionCall': {'id': 'call_1', 'name': 'lookup_ticket', 'args': args}, 'thoughtSignature': 'opaque'}] if tool else [{'text': text}]
    return {'candidates': [{'content': {'role': 'model', 'parts': parts}, 'finishReason': 'STOP'}],
            'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 2, 'totalTokenCount': 12}}


def run_response(check, payload, protocol='openai', status=200, headers=None):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload, headers=headers))) as client:
        return contract.run_one(client, settings(protocol), None, check, 1)


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_native_tool_roundtrip_preserves_full_assistant_and_matching_id(protocol):
    first = answer(protocol, tool=True)
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            assert 'status' in str(body)
            assert 'STATUS_' not in str(body)
            return httpx.Response(200, json=first)
        marker = re.search(r'STATUS_[0-9a-f]{32}', str(body)).group()
        if protocol == 'openai':
            assert body['messages'][-2] == first['choices'][0]['message']
            assert body['messages'][-1]['tool_call_id'] == 'call_1'
            assert body['messages'][-1]['role'] == 'tool'
        elif protocol == 'anthropic':
            assert body['messages'][-2]['content'] == first['content']
            assert body['messages'][-1]['content'][0]['tool_use_id'] == 'call_1'
        elif protocol == 'openai_responses':
            assert body['input'][-3:-1] == first['output']
            assert body['input'][-1]['call_id'] == 'call_1'
            assert body['input'][-1]['type'] == 'function_call_output'
        else:
            assert body['contents'][-2] == first['candidates'][0]['content']
            result = body['contents'][-1]['parts'][0]['functionResponse']
            assert result['id'] == 'call_1' and result['name'] == 'lookup_ticket'
            assert 'toolConfig' not in body
        return httpx.Response(200, json=answer(protocol, marker))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        item = contract.run_one(client, settings(protocol), None, 'tool_roundtrip', 1)
    assert item.verdict == 'conformed', item.detail
    assert item.requests == len(seen) == 2


@pytest.mark.parametrize('mutation', ['wrong_tool', 'wrong_ticket', 'extra_argument', 'invalid_json', 'duplicate_key', 'no_id', 'finish', 'two_calls'])
def test_invalid_tool_call_fails_without_second_request(mutation):
    first = answer('openai', tool=True)
    choice = first['choices'][0]
    call = choice['message']['tool_calls'][0]
    if mutation == 'wrong_tool':
        call['function']['name'] = 'get_time'
    elif mutation == 'wrong_ticket':
        call['function']['arguments'] = '{"ticket_id":"T-9999"}'
    elif mutation == 'extra_argument':
        call['function']['arguments'] = '{"ticket_id":"T-1024","extra":true}'
    elif mutation == 'invalid_json':
        call['function']['arguments'] = '```json\n{}\n```'
    elif mutation == 'duplicate_key':
        call['function']['arguments'] = '{"ticket_id":"wrong","ticket_id":"T-1024"}'
    elif mutation == 'no_id':
        call.pop('id')
    elif mutation == 'finish':
        choice['finish_reason'] = 'stop'
    else:
        choice['message']['tool_calls'].append(copy.deepcopy(call))
    item = run_response('tool_roundtrip', first)
    assert item.verdict == 'violated'
    assert item.requests == 1


@pytest.mark.parametrize('n', [1234, 9876])
@pytest.mark.parametrize('correct', [True, False])
def test_real_thinking_roundtrip_uses_actual_first_response(n, correct):
    first = answer('openai')
    message = first['choices'][0]['message']
    message.update(reasoning_content=f'Choose n = {n}. Final reply OK.', reasoning_signature='opaque', provider_extra={'x': 1})
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(200, json=first)
        assert body['messages'][-2] == message
        assert str(n) not in body['messages'][-1]['content']
        assert body['messages'][:-2] == seen[0]['messages']
        return httpx.Response(200, json=answer('openai', str(n * 2 if correct else n)))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        item = contract.run_one(client, settings(), None, 'thinking_roundtrip', 1)
    assert item.verdict == ('conformed' if correct else 'observation')
    assert item.requests == 2
    # Roundtrip evidence must keep both wire bodies, not only the follow-up.
    assert '=== 第 1 轮请求 ===' in item.request_body
    assert '=== 第 2 轮请求 ===' in item.request_body
    assert '把你上一轮取的 n 乘以 2' in item.request_body
    assert '=== 第 1 轮响应 ===' in item.response_body
    assert '=== 第 2 轮响应 ===' in item.response_body
    assert f'n = {n}' in item.response_body


@pytest.mark.parametrize('reasoning', ['', 'no chosen number', 'n = 1234, n = 5678'])
def test_missing_or_ambiguous_real_reasoning_aborts(reasoning):
    first = answer('openai')
    first['choices'][0]['message']['reasoning_content'] = reasoning
    item = run_response('thinking_roundtrip', first)
    assert item.verdict == 'violated' and item.requests == contract.INCONCLUSIVE_MAX_ATTEMPTS


def test_dynamic_and_top_level_tools_coexist():
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        assert body['tools'][0]['function']['name'] == 'get_time'
        assert body['messages'][-1]['tools'][0]['function']['name'] == 'lookup_ticket'
        assert body['messages'][-1].get('content') == ''
        assert body.get('tool_choice') == 'required'
        return httpx.Response(200, json=answer('openai', tool=True))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        item = contract.run_one(client, settings(), None, 'dynamic_tools_both', 1)
    assert item.verdict == 'conformed' and item.requests == len(seen) == 1


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'openai_responses'])
@pytest.mark.parametrize('check', ['cache_roundtrip', 'cache_roundtrip_stream'])
@pytest.mark.parametrize('read', [100, 0, -1, None, True])
def test_isolated_cache_write_wait_and_same_body_read(monkeypatch, protocol, check, read):
    seen, waits = [], []
    monkeypatch.setattr(contract, '_wait_cache', lambda seconds, stop: waits.append(seconds) or True)

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        data = answer(protocol)
        usage = data['usage']
        write_val = 100 if len(seen) == 1 else 0
        usage.update(cache_creation_input_tokens=write_val, cache_read_input_tokens=0 if len(seen) == 1 else read)
        if protocol == 'openai':
            usage['prompt_tokens_details'] = {'cache_write_tokens': write_val}
        elif protocol == 'openai_responses':
            usage['input_tokens_details'] = {'cache_write_tokens': write_val}
        usage['cache_creation'] = {'ephemeral_1h_input_tokens': write_val}
        headers = {'Msh-Usage-Cache-Write-Tokens-1h': str(write_val), 'Msh-Usage-Cache-Write-Tokens-5m': '0'}
        if body.get('stream'):
            chunk = {'choices': [{'delta': {'content': 'OK'}, 'finish_reason': 'stop'}], 'usage': usage}
            if protocol == 'anthropic':
                events = [
                    f'data: {json.dumps({"type": "message_start", "message": {"usage": usage}})}\n\n',
                    f'data: {json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "OK"}})}\n\n',
                    'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}\n\n',
                    'data: {"type": "message_stop"}\n\n',
                ]
            elif protocol == 'openai_responses':
                events = [
                    f'data: {json.dumps({"type": "response.output_text.delta", "delta": "OK"})}\n\n',
                    f'data: {json.dumps({"type": "response.completed", "response": data})}\n\n',
                ]
            else:
                events = [
                    f'data: {json.dumps(chunk)}\n\n',
                    'data: [DONE]\n\n',
                ]
            return httpx.Response(
                200,
                headers={**headers, 'Content-Type': 'text/event-stream'},
                content=''.join(events).encode(),
            )
        return httpx.Response(200, json=data, headers=headers)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        item = contract.run_one(client, settings(protocol), None, check, 1)
    assert item.verdict == ('conformed' if read == 100 else 'violated'), item.detail
    assert waits == [15]
    assert seen[0] == seen[1]
    assert seen[0]['stream'] is (check == 'cache_roundtrip_stream')
    # A real miss is retried once. This fixture's third call is no longer a cold write, so the
    # retry stops before a second wait.
    assert item.requests == (2 if read == 100 else 3)
    if read != 100:
        assert '连续 2 次复现' in item.detail


def test_failed_cache_write_aborts_and_wait_can_cancel(monkeypatch):
    monkeypatch.setattr(contract, '_wait_cache', lambda seconds, stop: pytest.fail('must not wait after failed write'))
    item = run_response('cache_roundtrip', answer('openai'))
    assert item.verdict == 'violated' and item.requests == 2
    assert '连续 2 次复现' in item.detail
    monkeypatch.setattr(contract, '_wait_cache', lambda seconds, stop: False)
    data = answer('openai')
    data['usage'].update(cache_creation_input_tokens=100, cache_read_input_tokens=0)
    item = run_response('cache_roundtrip', data, headers={'Msh-Usage-Cache-Write-Tokens-1h': '100'})
    assert item.verdict == 'inconclusive' and item.requests == 1


def test_cache_wait_is_at_least_fifteen_seconds_and_cancellable(monkeypatch):
    now, sleeps = [0.0], []

    def sleep(seconds):
        now[0] += seconds
        sleeps.append(seconds)

    monkeypatch.setattr(contract, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    assert contract._wait_cache(15, lambda: False)
    assert sum(sleeps) >= 15 and max(sleeps) <= 0.2
    assert not contract._wait_cache(15, lambda: True)
    with pytest.raises(ValidationError):
        settings(cache_wait_seconds=14)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('check', ['response_shape', 'usage'])
def test_valid_native_shapes_and_usage_pass(protocol, check):
    item = run_response(check, answer(protocol), protocol)
    assert item.verdict == 'conformed', item.detail
    assert item.requests == 1


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_wrong_protocol_and_malformed_shapes_fail(protocol):
    other = 'anthropic' if protocol == 'openai' else 'openai'
    assert run_response('response_shape', answer(other), protocol).verdict == 'violated'
    assert run_response('response_shape', {'choices': [False]}, protocol).verdict == 'violated'
    assert run_response('response_shape', [], protocol).verdict == 'violated'
    assert shape_errors(protocol, answer(protocol), 'text/html')


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('bad', [None, -1, True, '10', 1.2])
def test_usage_invalid_required_counters_fail(protocol, bad):
    payload = answer(protocol)
    key = 'usageMetadata' if protocol == 'gemini' else 'usage'
    field = {'openai': 'prompt_tokens', 'anthropic': 'input_tokens', 'openai_responses': 'input_tokens', 'gemini': 'promptTokenCount'}[protocol]
    payload[key][field] = bad
    assert run_response('usage', payload, protocol).verdict == 'violated'


def test_usage_totals_and_cache_bounds_are_checked():
    data = answer('openai')
    data['usage']['total_tokens'] = 99
    assert usage_errors('openai', data)
    data['usage']['total_tokens'] = 12
    data['usage']['prompt_tokens_details'] = {'cached_tokens': 11}
    assert usage_errors('openai', data)
    data['usage']['prompt_tokens_details']['cached_tokens'] = None
    assert not usage_errors('openai', data)


@pytest.mark.parametrize('text', ['Here: {"model":"A","price":1,"in_stock":true}',
    '```json\n{"model":"A","price":1,"in_stock":true}\n```',
    '{"model":"A","price":1,"in_stock":true} trailing',
    '{"model":"A","price":1,"price":2,"in_stock":true}',
    '{"model":"A","price":NaN,"in_stock":true}'])
def test_strict_json_rejects_wrappers_trailing_data_and_duplicates(text):
    assert run_response('json_schema', answer('openai', text)).verdict == 'violated'


@pytest.mark.parametrize('count', [25, 96, -1, True, '24'])
def test_output_limit_is_exact_and_counter_is_integer(count):
    data = answer('openai')
    data['usage']['completion_tokens'] = count
    assert run_response('max_tokens', data).verdict == 'violated'


def test_conflicting_system_markers_cannot_pass():
    assert run_response('system', answer('openai', 'SYSTEMOK USEROK')).verdict == 'violated'


def test_per_check_runs_and_counts_each_actual_request(monkeypatch):
    seen = []
    real_client = httpx.Client

    def respond(request):
        seen.append(request)
        path = str(request.url.path)
        proto = 'openai_responses' if path.endswith('/responses') else ('anthropic' if path.endswith('/messages') else 'openai')
        return httpx.Response(200, json=answer(proto, 'SYSTEMOK'))

    monkeypatch.setattr(contract.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(respond)))
    delivered = []
    items = contract.run_suite(settings(checks=['system', 'response_shape'], per_check=3), None, on_item=delivered.append)
    # 2 checks × 3 protocols × 3 repeats
    assert [item.index for item in items] == list(range(1, 19))
    assert [item.repeat for item in items] == [1, 2, 3, 1, 2, 3] * 3
    assert [item.protocol for item in items] == (
        ['openai'] * 6 + ['openai_responses'] * 6 + ['anthropic'] * 6
    )
    # system now spends a no-system baseline + main (2); response_shape stays 1.
    assert len(seen) == sum(item.requests for item in items) == 27
    assert delivered == items
    seen.clear()
    items = contract.run_suite(settings(checks=['system'], per_check=3), None, should_stop=lambda: len(seen) >= 1)
    assert len(items) == 1 and items[0].requests == 1


def test_all_registered_checks_have_titles_and_costs():
    assert set(CONTRACT_CHECKS) == set(contract.PROBES) == set(contract.TITLES) == set(contract.SEVERITY)
    for check in [
        'cache_roundtrip', 'cache_roundtrip_stream', 'cache_roundtrip_5m', 'cache_roundtrip_5m_stream',
        'thinking_roundtrip', 'tool_roundtrip',
    ]:
        assert contract.request_cost(check) == 2


@pytest.mark.parametrize('tail', ['', 'data: garbage\n\n', 'data: []\n\n', 'data: {"choices":false}\n\n',
    'data: {"error":{"message":"upstream died"}}\n\n', 'data: [DONE]\n\ndata: {"choices":[{"delta":{"content":"late"}}]}\n\n'])
def test_truncated_malformed_and_error_streams_never_pass(monkeypatch, tail):
    now = [0.0]
    monkeypatch.setattr(contract, 'time', SimpleNamespace(monotonic=lambda: now[0]))

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for n in range(3):
                now[0] += 0.1
                yield ('data: ' + json.dumps({'choices': [{'delta': {'content': str(n)}}]}) + '\n\n').encode()
            yield tail.encode()

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=Stream()))) as client:
        item = contract.run_one(client, settings(), None, 'stream', 1)
    assert item.verdict == 'violated', item.detail


@pytest.mark.parametrize('protocol', ['anthropic', 'openai_responses', 'gemini'])
def test_done_sentinel_does_not_replace_native_terminal(protocol):
    reply = protocols.Reply(status=200, chunks=3, content_chunk_times_ms=[0, 100, 200])
    protocols.parse_sse_line(settings(protocol), 'data: [DONE]', reply)
    assert contract._judge_stream(settings(protocol), reply, contract.PROBES['stream'])[0] == 'violated'


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_http_200_error_with_valid_usage_still_fails(protocol):
    payload = answer(protocol)
    payload['error'] = {'type': 'gateway_error', 'message': 'upstream failed'}
    assert run_response('usage', payload, protocol).verdict == 'violated'


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('bad', [False, 1, [], {}, 'bad'])
def test_malformed_nested_fields_cannot_crash_shape_check(protocol, bad):
    payload = answer(protocol)
    if protocol == 'openai':
        payload['choices'][0]['finish_reason'] = bad
        payload['choices'][0]['message']['content'] = [{'type': bad}]
    elif protocol == 'openai_responses':
        payload['status'] = bad
    elif protocol == 'anthropic':
        payload['content'] = [{'type': bad}]
    else:
        payload['candidates'][0]['content']['parts'] = [bad]
    assert run_response('response_shape', payload, protocol).verdict == 'violated'


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_tool_roundtrip_wrong_second_finish_fails(protocol):
    count = [0]

    def respond(request):
        count[0] += 1
        if count[0] == 1:
            return httpx.Response(200, json=answer(protocol, tool=True))
        marker = re.search(r'STATUS_[0-9a-f]{32}', request.content.decode()).group()
        payload = answer(protocol, marker)
        if protocol == 'openai':
            payload['choices'][0]['finish_reason'] = 'length'
        elif protocol == 'openai_responses':
            payload['status'] = 'incomplete'
            payload['incomplete_details'] = {'reason': 'max_output_tokens'}
        elif protocol == 'anthropic':
            payload['stop_reason'] = 'max_tokens'
        else:
            payload['candidates'][0]['finishReason'] = 'MAX_TOKENS'
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        item = contract.run_one(client, settings(protocol), None, 'tool_roundtrip', 1)
    assert item.verdict == 'violated' and item.requests == 2


def test_stream_multiline_data_is_one_event_and_duplicate_json_fails():
    data = 'data: {"choices":\ndata: [{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n'
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data, headers={'Content-Type': 'text/event-stream'}))) as client:
        reply = contract.call(client, settings(), None, {}, stream=True, timeout=5)
    assert reply.text == 'OK' and reply.chunks == 1 and reply.stream_completed
    assert not reply.stream_errors
    protocols.parse_sse_line(settings(), 'data: {"choices":[],"choices":[]}', reply)
    assert reply.stream_errors


def test_fast_and_full_modes_select_checks_and_protocols() -> None:
    """fast 模式只选核心高信号项与主协议，跳过 15s 缓存等待；full 保持全量。"""
    fast = settings(protocol='openai', mode='fast')
    full = settings(protocol='openai', mode='full')
    assert contract.select_protocols(fast) == ['openai']
    assert contract.select_protocols(full) == ['openai', 'openai_responses', 'anthropic']
    fast_checks = contract.select(fast)
    full_checks = contract.select(full)
    assert set(fast_checks) == set(FAST_CONTRACT_CHECKS)
    assert set(full_checks) == set(CONTRACT_CHECKS)
    assert 'cache_roundtrip' not in fast_checks
    assert 'cache_tools_append' not in fast_checks
    assert 'stream_tools' in full_checks and 'stream_tools' not in fast_checks
    assert 'fixed_sampling_params' in full_checks and 'fixed_sampling_params' not in fast_checks

    # 显式传入 checks / protocols 时优先尊重调用方覆盖
    custom = settings(protocol='openai', mode='fast', checks=['cache_roundtrip'], protocols=['anthropic'])
    assert contract.select(custom) == ['cache_roundtrip']
    assert contract.select_protocols(custom) == ['anthropic']

    # GLM 与 Generic 在 fast 模式下同样按自身 default_checks 过滤
    glm_fast = ContractSettings(url='https://fixture.test', model='glm-5.3', protocol='openai', mode='fast')
    assert 'dynamic_tools' not in contract.select(glm_fast)
    assert 'cache_headers' not in contract.select(glm_fast)
    assert 'system' in contract.select(glm_fast)


def test_stream_tools_validates_nested_schema_and_stream_completion() -> None:
    ok_args = json.dumps({
        'order_id': 'ORD-901',
        'priority': 'high',
        'destination': {'city': 'Beijing'},
        'items': [{'item_id': 'SKU-1', 'qty': 2}],
    })
    ok_reply = protocols.Reply(
        status=200,
        stream_completed=True,
        chunks=2,
        finish_reason='tool_calls',
        tool_calls=[{'id': 'call_1', 'name': contract.STREAM_TOOL_NAME, 'arguments': ok_args}],
    )
    probe = contract.PROBES['stream_tools']
    assert contract._judge_stream_tools(settings(), ok_reply, probe)[0] == 'conformed'

    bad_enum = copy.deepcopy(ok_reply)
    bad_enum.tool_calls[0]['arguments'] = json.dumps({
        'order_id': 'ORD-901',
        'priority': 'urgent',
        'destination': {'city': 'Beijing'},
        'items': [{'item_id': 'SKU-1', 'qty': 2}],
    })
    assert contract._judge_stream_tools(settings(), bad_enum, probe)[0] == 'violated'

    truncated = copy.deepcopy(ok_reply)
    truncated.stream_completed = False
    assert contract._judge_stream_tools(settings(), truncated, probe)[0] == 'violated'


def test_tool_schema_reject_and_reasoning_effort_invalid() -> None:
    body = contract._tool_schema_reject(settings(protocol='openai'), contract.PROBES['tool_schema_reject'])
    assert '$defs' in body['tools'][0]['function']['parameters']
    for protocol in ('openai_responses', 'anthropic'):
        with pytest.raises(protocols.Unsupported):
            contract._tool_schema_reject(settings(protocol=protocol), contract.PROBES['tool_schema_reject'])

    reject_400 = protocols.Reply(status=400, error='HTTP 400：recursive schema not allowed', error_type='invalid_request_error')
    assert contract._judge_expect_http_400(settings(), reject_400, contract.PROBES['tool_schema_reject'])[0] == 'conformed'
    assert contract._judge_expect_http_400(settings(), protocols.Reply(status=200, text='ok'), contract.PROBES['tool_schema_reject'])[0] == 'violated'

    effort_probe = contract.PROBES['reasoning_effort_invalid']
    assert contract._judge_reasoning_effort_invalid(settings(), reject_400, effort_probe)[0] == 'conformed'
    # Chat 官方实测本来就是 200，保持 observation；Responses 非法 reasoning.effort 返回 200 判 violated
    assert contract._judge_reasoning_effort_invalid(settings(), protocols.Reply(status=200, text='399'), effort_probe)[0] == 'observation'
    assert contract._judge_reasoning_effort_invalid(settings(protocol='openai_responses'), protocols.Reply(status=200, text='399'), effort_probe)[0] == 'violated'
    assert contract._judge_reasoning_effort_invalid(settings(), protocols.Reply(status=429, error='rate limit'), effort_probe)[0] == 'inconclusive'


def test_fixed_sampling_params_checks_all_four_immutable_knobs() -> None:
    seen_fields: list[dict] = []

    def respond_all_reject(request):
        body = json.loads(request.content)
        seen_fields.append(body)
        return httpx.Response(400, json={'error': {'type': 'invalid_request_error', 'message': 'immutable param'}})

    with httpx.Client(transport=httpx.MockTransport(respond_all_reject)) as client:
        item = contract.run_one(client, settings(), None, 'fixed_sampling_params', 1)
    assert item.verdict == 'conformed'
    assert item.requests == 4
    assert [
        ('top_p' in b, 'presence_penalty' in b, 'frequency_penalty' in b, 'n' in b)
        for b in seen_fields
    ] == [
        (True, False, False, False),
        (False, True, False, False),
        (False, False, True, False),
        (False, False, False, True),
    ]

    def respond_n_leaks(request):
        body = json.loads(request.content)
        if body.get('n') == 2:
            return httpx.Response(200, json=answer('openai'))
        return httpx.Response(400, json={'error': {'type': 'invalid_request_error', 'message': 'rejected'}})

    with httpx.Client(transport=httpx.MockTransport(respond_n_leaks)) as client:
        leaked = contract.run_one(client, settings(), None, 'fixed_sampling_params', 1)
    assert leaked.verdict == 'violated'
    assert 'n=2' in leaked.detail


def test_upstream_5xx_and_rate_limit_guards_on_choice_partial_and_json_judges() -> None:
    rate_limit = protocols.Reply(status=429, error='HTTP 429：rate limit')
    upstream_502 = protocols.Reply(status=502, error='bad gateway', error_type='upstream_error')
    hard_500 = protocols.Reply(status=500, error='internal remap')
    for judge, name in (
        (contract._judge_tool_choice_required, 'tool_choice_required'),
        (contract._judge_tool_choice_none, 'tool_choice_none'),
        (contract._judge_partial_mode, 'partial_mode'),
        (contract._judge_json_object, 'json_object'),
    ):
        probe = contract.PROBES[name]
        assert judge(settings(), rate_limit, probe)[0] == 'inconclusive'
        assert judge(settings(), upstream_502, probe)[0] == 'inconclusive'
        assert judge(settings(), hard_500, probe)[0] == 'violated'


def test_thinking_roundtrip_strictly_flags_dropped_context_and_accepts_markdown_n() -> None:
    assert contract._extract_roundtrip_n('我选 N = 4321，最终只输出 OK。') == {'4321'}
    assert contract._extract_roundtrip_n('假设取 `n` = `7342` 即可') == {'7342'}
    assert contract._extract_roundtrip_n('取 **n** 为 **8123**') == {'8123'}

    # 截图场景复现：kimi-k3.1-pro Chat 下首轮 n=7342，第二轮思考丢失后另猜 4321 输出 8642 → violated
    dropped = protocols.Reply(
        status=200,
        text='8642',
        reasoning='上一轮没有提供具体的 n，我按 4321 乘 2 得到 8642。',
        finish_reason='stop',
    )
    verdict, _, detail, observed = contract._judge_thinking_roundtrip_reply(
        settings(protocol='openai'), dropped, first_n='7342', expected='14684', finish='stop', severity='high',
    )
    assert verdict == 'violated'
    assert '首轮 n=7342' in observed

    # Anthropic Messages 协议纯文本多轮允许服务端省略历史 thinking 块 → 不得误判 violated，应记 observation
    anth_dropped = protocols.Reply(
        status=200,
        text='8642',
        reasoning='上一轮没有提供具体的 n，我按 4321 乘 2 得到 8642。',
        finish_reason='end_turn',
    )
    assert contract._judge_thinking_roundtrip_reply(
        settings(protocol='anthropic'), anth_dropped, first_n='7342', expected='14684', finish='end_turn', severity='high',
    )[0] == 'observation'

    # Kimi K2.5（允许关思考）普通文本轮跨轮保留思考非强制 → 未命中时记 observation
    k2_settings = ContractSettings(url='https://fixture.test', model='kimi-k2.5', protocol='openai')
    assert contract._judge_thinking_roundtrip_reply(
        k2_settings, dropped, first_n='7342', expected='14684', finish='stop', severity='high',
    )[0] == 'observation'

    # 第二轮思考无明显丢失话术但也完全没出现首轮 n=7342，且输出无关数字 8642 → Chat K3 violated
    silent_guess = protocols.Reply(
        status=200,
        text='8642',
        reasoning='取 4321 × 2 = 8642。',
        finish_reason='stop',
    )
    assert contract._judge_thinking_roundtrip_reply(
        settings(protocol='openai'), silent_guess, first_n='7342', expected='14684', finish='stop', severity='high',
    )[0] == 'violated'

    # 若第二轮思考明确引用了首轮 7342 但算错成 14694，保留为 observation（不误杀）
    arithmetic_slip = protocols.Reply(
        status=200,
        text='14694',
        reasoning='首轮取了 7342，7342 * 2 = 14694。',
        finish_reason='stop',
    )
    assert contract._judge_thinking_roundtrip_reply(
        settings(protocol='openai'), arithmetic_slip, first_n='7342', expected='14684', finish='stop', severity='high',
    )[0] == 'observation'

    # 结束原因异常或含工具调用 → 即便在 Anthropic 协议下也必须判 violated；纯数字带句号仍算 conformed
    truncated = protocols.Reply(status=200, text='14684', finish_reason='length')
    assert contract._judge_thinking_roundtrip_reply(
        settings(protocol='anthropic'), truncated, first_n='7342', expected='14684', finish='end_turn', severity='high',
    )[0] == 'violated'
    punctuated = protocols.Reply(status=200, text='14684。', finish_reason='stop')
    assert contract._judge_thinking_roundtrip_reply(
        settings(protocol='openai'), punctuated, first_n='7342', expected='14684', finish='stop', severity='high',
    )[0] == 'conformed'


def test_preserved_thinking_strictly_flags_dropped_reasoning() -> None:
    probe = contract.PROBES['preserved_thinking']
    openai_k3 = settings(protocol='openai')

    # 截图场景：思考被中转站剥离后，模型明确说没有记录 n 并另猜 n=4321 输出 8642 → violated
    dropped = protocols.Reply(
        status=200,
        text='8642',
        reasoning='我们没有实际记录n，思考不可见。可以任意选择n=4321，结果8642。',
        finish_reason='stop',
    )
    verdict, severity, detail, observed = contract._judge_preserved_thinking(openai_k3, dropped, probe)
    assert verdict == 'violated'
    assert severity == 'high'
    assert '4287' in detail
    assert observed == '8642'

    # 思考里未出现显式“丢失”话术，但也完全没引用注入的 4287 → violated
    silent_guess = protocols.Reply(
        status=200,
        text='8642',
        reasoning='取 n = 4321，乘以 2 得 8642。',
        finish_reason='stop',
    )
    assert contract._judge_preserved_thinking(openai_k3, silent_guess, probe)[0] == 'violated'

    # 明确引用了注入的 4287 但模型自己算错成 8564 → 保留 observation
    arithmetic_slip = protocols.Reply(
        status=200,
        text='8564',
        reasoning='第 1 步取了 n = 4287，4287 * 2 = 8564。',
        finish_reason='stop',
    )
    assert contract._judge_preserved_thinking(openai_k3, arithmetic_slip, probe)[0] == 'observation'

    # 未出现思考文本且仅微幅算错（8572 vs 8574）→ observation
    close_slip = protocols.Reply(status=200, text='8572', finish_reason='stop')
    assert contract._judge_preserved_thinking(openai_k3, close_slip, probe)[0] == 'observation'

    # 带标点或截断/工具调用的边界行为
    assert contract._judge_preserved_thinking(
        openai_k3, protocols.Reply(status=200, text='8574。', finish_reason='stop'), probe,
    )[0] == 'conformed'
    assert contract._judge_preserved_thinking(
        openai_k3, protocols.Reply(status=200, text='8574', finish_reason='length'), probe,
    )[0] == 'violated'
    assert contract._judge_preserved_thinking(
        openai_k3,
        protocols.Reply(status=200, text='', tool_calls=[{'name': 'lookup_ticket', 'arguments': '{}'}]),
        probe,
    )[0] == 'violated'

    # GLM（默认 clear_thinking=False）同样判 violated；Anthropic / Kimi K2.5 记为 observation
    glm_settings = ContractSettings(url='https://fixture.test', model='glm-5.3', protocol='openai')
    assert contract._judge_preserved_thinking(glm_settings, dropped, probe)[0] == 'violated'
    anth_dropped = protocols.Reply(
        status=200,
        text='8642',
        reasoning=dropped.reasoning,
        finish_reason='end_turn',
    )
    assert contract._judge_preserved_thinking(settings(protocol='anthropic'), anth_dropped, probe)[0] == 'observation'
    k2_settings = ContractSettings(url='https://fixture.test', model='kimi-k2.5', protocol='openai')
    assert contract._judge_preserved_thinking(k2_settings, dropped, probe)[0] == 'observation'


def test_responses_vision_svg_reject_sends_input_image_block() -> None:
    """#87: Responses build_body 必须包含 input_image 块，且通过前置断言。"""
    resp_settings = settings(protocol='openai_responses')
    probe = contract.PROBES['vision_svg_reject']
    body = contract._vision_svg_reject(resp_settings, probe)
    parts = body['input'][-1]['content']
    assert parts[0] == {'type': 'input_text', 'text': probe.prompt}
    assert parts[1]['type'] == 'input_image'
    assert parts[1]['image_url'].startswith('data:image/svg+xml;base64,')


def test_partial_json_mix_validates_prefix_concatenation() -> None:
    """#53: 只有真正承接 prefix 的续写且拼接后 ok==true 才算通过；原样输出 {...} 判不通过。"""
    chat = settings(protocol='openai')
    probe = contract.PROBES['partial_json_mix']
    assert contract._judge_partial_json_mix(chat, protocols.Reply(status=200, text='true}'), probe)[0] == 'conformed'
    assert contract._judge_partial_json_mix(chat, protocols.Reply(status=200, text=' true }'), probe)[0] == 'conformed'
    guessed = protocols.Reply(status=200, text='true}', reasoning='用户要 ok=true，所以补 true}')
    guessed_verdict, _, guessed_detail, _ = contract._judge_partial_json_mix(chat, guessed, probe)
    assert guessed_verdict == 'violated'
    assert 'reasoning_content' in guessed_detail
    assert contract._judge_partial_json_mix(chat, protocols.Reply(status=200, text='{"ok":true}'), probe)[0] == 'violated'
    assert contract._judge_partial_json_mix(chat, protocols.Reply(status=200, text='false}'), probe)[0] == 'violated'
    assert contract._judge_partial_json_mix(chat, protocols.Reply(status=200, text='not-json'), probe)[0] == 'violated'
    assert contract._judge_partial_json_mix(
        chat,
        protocols.Reply(status=400, error='HTTP 400：partial not allowed', error_type='invalid_request_error'),
        probe,
    )[0] == 'conformed'


def test_responses_stream_accepts_incomplete_terminal_and_raises_budget() -> None:
    """#75: Responses SSE 允许 response.incomplete / status=incomplete 终态，且 stream 上限至少 2048/4096。"""
    resp_settings = settings(protocol='openai_responses')
    stream_body = contract._stream(resp_settings, contract.PROBES['stream'])
    assert stream_body['max_output_tokens'] == 4096
    generic_stream = contract._stream(
        ContractSettings(url='https://fixture.test', model='gpt-4o', protocol='openai_responses'),
        contract.PROBES['stream'],
    )
    assert generic_stream['max_output_tokens'] == 2048

    for terminal in (
        {'type': 'response.incomplete', 'response': {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'}}},
        {'type': 'response.completed', 'response': {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'}}},
    ):
        reply = protocols.Reply()
        for idx, ch in enumerate(('一', '二', '三')):
            protocols.parse_sse_line(
                resp_settings,
                'data: ' + json.dumps({'type': 'response.output_text.delta', 'delta': ch}, ensure_ascii=False),
                reply,
                elapsed_ms=100 + idx * 50,
            )
        protocols.parse_sse_line(resp_settings, 'data: ' + json.dumps(terminal, ensure_ascii=False), reply, elapsed_ms=300)
        assert reply.stream_completed is True
        assert not reply.stream_errors
        assert reply.finish_reason == 'length'
        assert contract._judge_stream(resp_settings, reply, contract.PROBES['stream'])[0] == 'conformed'


def test_tool_choice_required_400_is_violated_and_stream_tools_followup_avoid_required() -> None:
    """#61/#63/#64/#73: Kimi 官方 /v1/responses 不支持 required/none（预期 400，中转 200 记差异）；Chat 仍要求 200。"""
    resp_settings = settings(protocol='openai_responses')
    stream_body = contract._stream_tools(resp_settings, contract.PROBES['stream_tools'])
    assert stream_body.get('tool_choice') != 'required'
    followup_body = contract._tool_followup(resp_settings, contract.PROBES['tool_followup'])
    assert followup_body.get('tool_choice') != 'required'

    reject_400 = protocols.Reply(
        status=400,
        error='HTTP 400：responses: unsupported tool_choice value: "required"; use "auto"',
        error_type='invalid_request_error',
    )
    assert contract._judge_tool_choice_required(resp_settings, reject_400, contract.PROBES['tool_choice_required'])[0] == 'conformed'
    assert contract._judge_tool_choice_none(resp_settings, reject_400, contract.PROBES['tool_choice_none'])[0] == 'conformed'
    assert contract._judge_tool_choice_required(resp_settings, protocols.Reply(status=200, tool_calls=[{'name': 'lookup_ticket'}]), contract.PROBES['tool_choice_required'])[0] == 'observation'
    assert contract._judge_tool_choice_none(resp_settings, protocols.Reply(status=200, text='OK'), contract.PROBES['tool_choice_none'])[0] == 'observation'
    # Chat 协议下 required/none 仍为官方支持，被 400 拒绝仍判 violated
    chat_settings = settings(protocol='openai')
    assert contract._judge_tool_choice_required(chat_settings, reject_400, contract.PROBES['tool_choice_required'])[0] == 'violated'
    assert contract._judge_tool_choice_none(chat_settings, reject_400, contract.PROBES['tool_choice_none'])[0] == 'violated'
    assert contract._judge_stream_tools(resp_settings, reject_400, contract.PROBES['stream_tools'])[0] == 'violated'


def test_cache_options_echo_violated_when_input_reaches_block_threshold_without_write() -> None:
    """#86/#92: 达块阈值却既无回显也无写入判 violated；正写档且两档写头正常但未回显仅记 observation（P2 低危）。"""
    resp_settings = settings(protocol='openai_responses')
    probe = contract.PROBES['cache_options_echo']
    large_no_write = protocols.Reply(
        status=200,
        payload={'output_text': 'OK'},
        usage=protocols.Usage(prompt_tokens=1943, cache_write_tokens=None, cached_tokens=0),
    )
    assert contract._judge_cache_options_echo(resp_settings, large_no_write, probe)[0] == 'violated'
    write_with_valid_headers = protocols.Reply(
        status=200,
        payload={'output_text': 'OK'},
        headers={'msh-usage-cache-write-tokens-5m': '1200', 'msh-usage-cache-write-tokens-1h': '0'},
        usage=protocols.Usage(prompt_tokens=1943, cache_write_tokens=1200, cached_tokens=0),
    )
    assert contract._judge_cache_options_echo(resp_settings, write_with_valid_headers, probe)[0] == 'observation'
    echoed_ok = protocols.Reply(
        status=200,
        payload={'output_text': 'OK', 'prompt_cache_options': {'mode': 'implicit', 'ttl': '5m'}},
        usage=protocols.Usage(prompt_tokens=512, cache_write_tokens=0, cached_tokens=0),
    )
    assert contract._judge_cache_options_echo(resp_settings, echoed_ok, probe)[0] == 'conformed'


def test_responses_reasoning_leaked_into_output_text_is_flagged() -> None:
    """#68: Responses 的 output_text 与 reasoning 相同或大段重合时判 violated。"""
    leaked_text = 'Thinking Process: 1. Understand the goal. 2. Describe how rivers shape landforms.'
    payload = {
        'id': 'resp_leak',
        'object': 'response',
        'model': 'fixture',
        'status': 'incomplete',
        'incomplete_details': {'reason': 'max_output_tokens'},
        'output': [
            {'id': 'rs_1', 'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': leaked_text}]},
            {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': leaked_text}]},
        ],
        'usage': {'input_tokens': 20, 'output_tokens': 20, 'total_tokens': 40},
    }
    assert any('output_text' in err for err in shape_errors('openai_responses', payload, 'application/json'))
    item = run_response('max_tokens', payload, protocol='openai_responses')
    assert item.verdict == 'violated'
    assert 'reasoning' in item.detail

    # 正常短答案如 OK 不应被误判
    normal_payload = copy.deepcopy(payload)
    normal_payload['status'] = 'completed'
    normal_payload.pop('incomplete_details', None)
    normal_payload['output'][0]['summary'][0]['text'] = 'The user asked to reply OK, so I will output OK.'
    normal_payload['output'][1]['content'][0]['text'] = 'OK'
    assert not shape_errors('openai_responses', normal_payload, 'application/json')


def test_vision_content_string_requires_passcode_for_conformed() -> None:
    """#44: content 序列化字符串若未读出 5827，仅记为 observation 而非 conformed。"""
    chat = settings(protocol='openai')
    probe = contract.PROBES['vision_content_string']
    read_img = protocols.Reply(status=200, text='5827', usage=protocols.Usage(prompt_tokens=280))
    assert contract._judge_vision_accept(chat, read_img, probe)[0] == 'conformed'
    plain_text = protocols.Reply(status=200, text='我没有看到图片', usage=protocols.Usage(prompt_tokens=2599))
    verdict, _, detail, observed = contract._judge_vision_accept(chat, plain_text, probe)
    assert verdict == 'observation'
    assert '5827' in detail
    assert 'prompt_tokens=2599' in observed


def test_thinking_disable_distinguishes_stripped_reasoning_from_disabled() -> None:
    """#49-#51: reasoning_len==0 时若 completion_tokens 远超正文长度，提示思考块被剥离。"""
    chat = settings(protocol='openai')
    probe = contract.PROBES['thinking_disable']
    stripped = protocols.Reply(status=200, text='391', reasoning='', usage=protocols.Usage(completion_tokens=70))
    verdict, _, detail, observed = contract._judge_thinking_disable(chat, stripped, probe)
    assert verdict == 'violated'
    assert '剥离' in detail
    assert 'completion_tokens=70' in observed

    disabled = protocols.Reply(status=200, text='391', reasoning='', usage=protocols.Usage(completion_tokens=2))
    verdict2, _, detail2, _ = contract._judge_thinking_disable(chat, disabled, probe)
    assert verdict2 == 'violated'
    assert '静默关掉' in detail2


def test_thinking_roundtrip_fallback_extracts_unique_4digit_when_text_is_ok() -> None:
    """#22/#104: 正文严格为 OK 时，支持从 reasoning 兜底提取唯一非 1000/9999 四位数。"""
    first = answer('openai', text='OK')
    first['choices'][0]['message']['reasoning_content'] = '从 1000 到 9999 之间选一个整数，比如 3726，千位是 3。'
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(200, json=first)
        return httpx.Response(200, json=answer('openai', text='7452'))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        item = contract.run_one(client, settings(), None, 'thinking_roundtrip', 1)
    assert item.verdict == 'conformed'
    assert item.requests == 2

    # 正文漏出 n = 3726（非 OK）时连续重试 3 次仍不按指令输出，最终判定 violated
    leaked_text = answer('openai', text='n = 3726')
    leaked_text['choices'][0]['message']['reasoning_content'] = '比如 3726'
    leaked_item = run_response('thinking_roundtrip', leaked_text)
    assert leaked_item.verdict == 'violated' and leaked_item.requests == contract.INCONCLUSIVE_MAX_ATTEMPTS


def test_model_behavior_inconclusive_retries_and_passes_or_fails() -> None:
    """模型行为导致无法判定时应重复发送请求：若后续通过则判通过，连续失败则判不通过。"""
    bad_first = answer('openai', text='n = 3726')
    bad_first['choices'][0]['message']['reasoning_content'] = 'n = 3726'
    good_first = answer('openai', text='OK')
    good_first['choices'][0]['message']['reasoning_content'] = 'n = 3726'

    calls = 0

    def flaky_then_ok(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=bad_first)
        if calls == 2:
            return httpx.Response(200, json=good_first)
        return httpx.Response(200, json=answer('openai', text='7452'))

    with httpx.Client(transport=httpx.MockTransport(flaky_then_ok)) as client:
        item = contract.run_one(client, settings(), None, 'thinking_roundtrip', 1)
    assert item.verdict == 'conformed'
    assert item.requests == 3
    assert '第 2 次尝试' in item.detail

    # 连续 3 次均未能通过 → violated
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=bad_first))) as client:
        failed = contract.run_one(client, settings(), None, 'thinking_roundtrip', 1)
    assert failed.verdict == 'violated'
    assert failed.requests == contract.INCONCLUSIVE_MAX_ATTEMPTS
    assert '连续 3 次请求' in failed.detail


def test_network_inconclusive_retries_and_recovers_or_stays_inconclusive(monkeypatch) -> None:
    """网络/限流/上游临时故障导致无法判定时应重试；恢复则正常判定，全失败保持 inconclusive。"""
    monkeypatch.setattr(contract.time, 'sleep', lambda _s: None)
    calls = 0

    def transient_then_ok(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError('temporary network blip')
        return httpx.Response(200, json=answer('openai', text='反证法证明。') | {
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '反证法证明。', 'reasoning_content': '先分析'}, 'finish_reason': 'stop'}],
        })

    with httpx.Client(transport=httpx.MockTransport(transient_then_ok)) as client:
        recovered = contract.run_one(client, settings(), None, 'thinking', 1)
    assert recovered.verdict == 'conformed'
    assert recovered.requests == 3

    def always_timeout(request):
        raise httpx.ReadTimeout('timed out')

    with httpx.Client(transport=httpx.MockTransport(always_timeout)) as client:
        still_down = contract.run_one(client, settings(), None, 'thinking', 1)
    assert still_down.verdict == 'inconclusive'
    assert still_down.requests == contract.INCONCLUSIVE_MAX_ATTEMPTS
    assert '已重试 3 次' in still_down.detail


def test_reasoning_drafting_clean_output_does_not_false_positive_as_leak() -> None:
    """#66/#72/#75：思考草稿中预演了干净的正文（JSON、随机 status 或短散文）不应误判为思考泄漏。"""
    from relay_lab.protocol_validation import reasoning_leaked_into_text

    # #66 json_schema
    assert not reasoning_leaked_into_text(
        'I can output {"model":"MacBook Air 13","price":8999,"in_stock":true}. Make sure raw JSON.',
        '{"model":"MacBook Air 13","price":8999,"in_stock":true}',
    )
    # #72 tool_roundtrip status
    assert not reasoning_leaked_into_text(
        '工具返回了 status 字段值为 "STATUS_4376441060ca4ee7a46ac15278d41501"。用户要求只输出 status 字段值。',
        'STATUS_4376441060ca4ee7a46ac15278d41501',
    )
    # #75 stream prose
    prose = '清晨，薄雾缠绕山腰，露珠在草叶上闪光。溪水轻唱，鸟声穿过松林，东方渐染金色。'
    assert not reasoning_leaked_into_text(f'可以写：“{prose}”数字数。', prose)

    # 真正的泄漏（正文等于思考、思考文本包含于正文、或长前缀重合）仍必须命中
    assert reasoning_leaked_into_text('I should respond only ROUTING_OK.', 'I should respond only ROUTING_OK.')
    assert reasoning_leaked_into_text('Thinking Process: draft answer', 'Thinking Process: draft answer\nFinal: OK')
    assert reasoning_leaked_into_text('A' * 45 + ' tail1', 'A' * 45 + ' tail2')


def test_responses_sse_does_not_duplicate_reasoning_on_output_item_done() -> None:
    """#61/#75/#76：Responses 流式收到 delta 后，output_item.done 不得重复拼接 reasoning。"""
    resp_settings = settings(protocol='openai_responses')
    reply = protocols.Reply()
    events = [
        {'type': 'response.reasoning_summary_text.delta', 'item_id': 'rs_1', 'delta': '先分析'},
        {'type': 'response.reasoning_summary_text.delta', 'item_id': 'rs_1', 'delta': '再作答'},
        {'type': 'response.output_item.done', 'item': {'id': 'rs_1', 'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': '先分析再作答'}]}},
        {'type': 'response.output_text.delta', 'delta': 'OK'},
        {'type': 'response.completed', 'response': {'status': 'completed', 'usage': {'input_tokens': 10, 'output_tokens': 5}}},
    ]
    for index, event in enumerate(events):
        protocols.parse_sse_line(resp_settings, 'data: ' + json.dumps(event), reply, elapsed_ms=(index + 1) * 10)
    assert reply.reasoning == '先分析再作答'
    assert reply.text == 'OK'
    assert reply.chunks == 3  # 两次 reasoning delta + 一次 text delta，done 不应再计入

    # 若流中未出现任何 reasoning delta，output_item.done 仍应兜底补齐 reasoning
    fallback_reply = protocols.Reply()
    protocols.parse_sse_line(
        resp_settings,
        'data: ' + json.dumps({'type': 'response.output_item.done', 'item': {'id': 'rs_2', 'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': '仅在 done 给出的思考'}]}}),
        fallback_reply,
        elapsed_ms=10,
    )
    assert fallback_reply.reasoning == '仅在 done 给出的思考'


def test_anthropic_sse_reads_input_tokens_from_message_delta_when_start_is_zero() -> None:
    """#110：message_start 的 input_tokens=0 时应采纳 message_delta 中的正数输入计数，且不反向覆盖正数。"""
    anth_settings = settings(protocol='anthropic')
    reply = protocols.Reply()
    for event in [
        {'type': 'message_start', 'message': {'usage': {'input_tokens': 0}}},
        {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '1'}},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'input_tokens': 99, 'output_tokens': 78}},
        {'type': 'message_stop'},
    ]:
        protocols.parse_event(anth_settings, event, reply)
    assert reply.usage.prompt_tokens == 99
    assert reply.usage.completion_tokens == 78

    # 标准 Anthropic 在 message_start 已有正数且 message_delta 给 0 时，不得被 0 覆盖
    standard_reply = protocols.Reply()
    for event in [
        {'type': 'message_start', 'message': {'usage': {'input_tokens': 42}}},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'input_tokens': 0, 'output_tokens': 5}},
    ]:
        protocols.parse_event(anth_settings, event, standard_reply)
    assert standard_reply.usage.prompt_tokens == 42


def test_anthropic_cache_headers_and_tools_prefix_threshold_diagnostics() -> None:
    """Anthropic cache 不应报缺失 Msh-* 响应头；tools 前缀不足 1024 token 时应给出明确提示。"""
    assert contract._missing_response_headers('cache_headers', {}, protocol='anthropic') == []
    assert contract._missing_response_headers('cache_headers', {}, protocol='openai') == ['msh-usage-cache-write-tokens-1h']

    anth_settings = settings(protocol='anthropic')
    warm = protocols.Reply(status=200, text='收到', usage=protocols.Usage(prompt_tokens=917, cached_tokens=511))
    hot = protocols.Reply(status=200, text='收到', usage=protocols.Usage(prompt_tokens=66, cached_tokens=1362))
    _, _, _, observed = contract._judge_cache(anth_settings, hot, contract.PROBES['cache'], warm)
    assert 'prompt=1428' in observed

    chat_settings = settings(protocol='openai')
    short_tools = protocols.Reply(
        status=200,
        headers={'msh-usage-cache-write-tokens-5m': '0'},
        usage=protocols.Usage(prompt_tokens=805, cache_write_tokens=0, cached_tokens=0),
    )
    verdict, _, detail, _ = contract._judge_cache_headers(chat_settings, short_tools, contract.PROBES['cache_tools_prefix'])
    assert verdict == 'violated'
    assert '未达最小缓存块阈值' in detail


def test_partial_mode_requires_exact_suffix_and_empty_reasoning() -> None:
    """Partial 用强反差 JSON 前缀：正文必须恰好是 2}，且 reasoning_content 为空。"""
    chat_settings = settings(protocol='openai')
    probe = contract.PROBES['partial_mode']
    body = contract._partial_mode(chat_settings, probe)
    assert body['messages'][0]['content'] == '1+1等于几？'
    assert body['messages'][-1]['content'] == contract._PARTIAL_MODE_PREFIX
    assert body['messages'][-1]['partial'] is True
    assert contract._judge_partial_mode(
        chat_settings, protocols.Reply(status=200, text='2}'), probe,
    )[0] == 'conformed'
    guessed = protocols.Reply(status=200, text='2}', reasoning='先算 1+1')
    assert contract._judge_partial_mode(chat_settings, guessed, probe)[0] == 'violated'
    rewritten = protocols.Reply(status=200, text='{"prefix_check":"MUST_KEEP","answer":2}')
    assert contract._judge_partial_mode(chat_settings, rewritten, probe)[0] == 'violated'


def test_dynamic_tools_mutex_accepts_must_be_empty_phrasing() -> None:
    """#10：网关返回 'system content must be empty with dynamic tools' 应识别为符合互斥拒绝契约。"""
    chat_settings = settings(protocol='openai')
    reply = protocols.Reply(
        status=400,
        error='HTTP 400：system content must be empty with dynamic tools',
        error_type='invalid_request_error',
        error_code='invalid_request',
    )
    verdict, _, detail, _ = contract._judge_dynamic_tools_mutex(
        chat_settings, reply, contract.PROBES['dynamic_tools_mutex']
    )
    assert verdict == 'conformed'
    assert '差异告警' not in detail


def test_anthropic_message_without_content_still_parses_usage_and_stop_reason() -> None:
    """#99：Anthropic 非流式响应省略 content 数组时，仍应解析 usage 与 stop_reason。"""
    payload = {
        'id': 'chatcmpl-7418d08c',
        'type': 'message',
        'role': 'assistant',
        'stop_reason': 'max_tokens',
        'model': 'kimi-k3.1-pro',
        'usage': {'input_tokens': 101, 'output_tokens': 24, 'output_tokens_details': {'thinking_tokens': 16}},
    }
    item = run_response('max_tokens', payload, protocol='anthropic')
    assert item.verdict == 'conformed'
    assert item.requests == 1


def test_thinking_roundtrip_stripped_thinking_reports_gateway_omission() -> None:
    """#104：首轮仅返回 OK 且思考内容为空时，提示可能被网关剥离而非归咎于模型未按要求输出。"""
    stripped = {
        'id': 'msg_1',
        'type': 'message',
        'role': 'assistant',
        'content': [{'type': 'text', 'text': 'OK'}],
        'stop_reason': 'end_turn',
        'model': 'kimi-k3.1-pro',
        'usage': {'input_tokens': 179, 'output_tokens': 152},
    }
    item = run_response('thinking_roundtrip', stripped, protocol='anthropic')
    assert item.verdict == 'violated'
    assert item.requests == contract.INCONCLUSIVE_MAX_ATTEMPTS
    assert '可能被网关剥离' in item.detail
    assert '模型未按要求输出' not in item.detail


def test_kimi_responses_json_object_and_tool_choice_expect_400_and_flag_200_as_observation() -> None:
    """一、Kimi 官方 /v1/responses 不支持 json_object / tool_choice=required / none：预期 400，中转 200 记差异。"""
    resp_settings = settings(protocol='openai_responses')
    ok_400 = {'error': {'type': 'invalid_request_error', 'message': 'unsupported text.format type "json_object"'}}
    item_400 = run_response('json_object', ok_400, protocol='openai_responses', status=400)
    assert item_400.verdict == 'conformed', item_400.detail

    item_200 = run_response('json_object', answer('openai_responses', text='{"ok": true}'), protocol='openai_responses', status=200)
    assert item_200.verdict == 'observation'
    assert '额外兼容/协议转译差异' in item_200.detail

    # 错误的 error.type 即使 HTTP 400 也不能通过
    bad_type_400 = {'error': {'type': '400001', 'message': 'unsupported'}}
    assert run_response('json_object', bad_type_400, protocol='openai_responses', status=400).verdict == 'violated'
    assert run_response('tool_choice_required', bad_type_400, protocol='openai_responses', status=400).verdict == 'violated'
    assert run_response('tool_choice_none', bad_type_400, protocol='openai_responses', status=400).verdict == 'violated'


@pytest.mark.parametrize(
    ('protocol', 'expected_field'),
    [
        ('openai', 'usage.prompt_tokens_details.cache_write_tokens'),
        ('openai_responses', 'usage.input_tokens_details.cache_write_tokens'),
        ('anthropic', 'usage.output_tokens_details.thinking_tokens'),
    ],
)
def test_kimi_global_200_billing_usage_fields_are_mandatory(protocol: str, expected_field: str) -> None:
    """二.1：Kimi usage 专项强校验必填计费细分字段，且不连坐其他业务/结构用例。"""
    payload = answer(protocol, text='OK')
    payload['usage'].pop('prompt_tokens_details', None)
    payload['usage'].pop('input_tokens_details', None)
    payload['usage'].pop('output_tokens_details', None)
    usage_item = run_response('usage', payload, protocol=protocol)
    assert usage_item.verdict == 'violated'
    assert usage_item.severity == 'high'
    assert expected_field in usage_item.detail
    # 业务/结构用例不被连坐
    shape_item = run_response('response_shape', payload, protocol=protocol)
    assert shape_item.verdict == 'conformed'


@pytest.mark.parametrize('protocol', ['openai', 'openai_responses'])
def test_kimi_global_implicit_cache_write_headers_required_and_summed(protocol: str) -> None:
    """二.2：Chat/Responses 写档响应头专项检查：cache_write_tokens > 0 必含 5m/1h 且之和相等，不连坐业务用例。"""
    payload = answer(protocol, text='OK')
    if protocol == 'openai':
        payload['usage']['prompt_tokens_details']['cache_write_tokens'] = 640
    else:
        payload['usage']['input_tokens_details']['cache_write_tokens'] = 640

    # 单条业务/结构用例不被连坐
    assert run_response('response_shape', payload, protocol=protocol, headers={}).verdict == 'conformed'

    # 专项项 cache_write_headers_audit 准确捕获缺头与求和不符
    missing_item = run_response('cache_write_headers_audit', payload, protocol=protocol, headers={})
    assert missing_item.verdict == 'violated'
    assert 'Msh-Usage-Cache-Write-Tokens-5m' in missing_item.detail

    bad_sum = run_response(
        'cache_write_headers_audit',
        payload,
        protocol=protocol,
        headers={'Msh-Usage-Cache-Write-Tokens-5m': '500', 'Msh-Usage-Cache-Write-Tokens-1h': '100'},
    )
    assert bad_sum.verdict == 'violated'
    assert '不一致' in bad_sum.detail

    ok_sum = run_response(
        'cache_write_headers_audit',
        payload,
        protocol=protocol,
        headers={'Msh-Usage-Cache-Write-Tokens-5m': '640', 'Msh-Usage-Cache-Write-Tokens-1h': '0'},
    )
    assert ok_sum.verdict == 'conformed', ok_sum.detail


def test_kimi_responses_fixed_attrs_enforced_globally() -> None:
    """二.3：Responses response_shape 强校验 4 固定属性与 resp_chat_ 转译检测，不连坐其他用例。"""
    bad_store = answer('openai_responses', text='OK')
    bad_store['store'] = None
    item = run_response('response_shape', bad_store, protocol='openai_responses')
    assert item.verdict == 'violated'
    assert 'store' in item.detail

    missing_keys = answer('openai_responses', text='OK')
    del missing_keys['background']
    del missing_keys['previous_response_id']
    del missing_keys['conversation']
    # usage 专项只管计费字段，不被 Responses 固定属性缺失连坐
    assert run_response('usage', missing_keys, protocol='openai_responses').verdict == 'conformed'
    # response_shape 专项准确捕获全部缺失字段
    shape_item = run_response('response_shape', missing_keys, protocol='openai_responses')
    assert shape_item.verdict == 'violated'
    assert 'background' in shape_item.detail
    assert 'previous_response_id' in shape_item.detail
    assert 'conversation' in shape_item.detail

    # output[].id 带 resp_chat_ 前缀直接标记为 Responses 转 Chat 协议转译
    translated = answer('openai_responses', text='OK')
    translated['output'][0]['id'] = 'resp_chat_001'
    item3 = run_response('response_shape', translated, protocol='openai_responses')
    assert item3.verdict == 'violated'
    assert 'Responses 转 Chat 协议转译' in item3.detail
    assert 'resp_chat_001' in item3.detail


def test_400_error_machine_fields_strict_type_and_warnings() -> None:
    """二.4：400 用例强校验 error.type=invalid_request_error，并对数字 code / 空 param / 兜底文案标记差异告警。"""
    # error.type = "400001" 直接判 violated
    bad_type = {'error': {'type': '400001', 'message': 'bad request'}}
    item_bad = run_response('tool_choice_required_bare', bad_type, protocol='openai', status=400)
    assert item_bad.verdict == 'violated'
    assert 'invalid_request_error' in item_bad.detail

    # error.type 正确，但含内部数字 code + param="" + 通用兜底文案 → conformed + 差异告警
    warn_400 = {'error': {'type': 'invalid_request_error', 'code': '400001', 'param': '', 'message': 'Invalid request'}}
    item_warn = run_response('tool_choice_required_bare', warn_400, protocol='openai', status=400)
    assert item_warn.verdict == 'conformed'
    assert '差异告警' in item_warn.detail
    assert '400001' in item_warn.detail
    assert 'param' in item_warn.detail


def test_reasoning_effort_invalid_covers_responses_and_rejects_200() -> None:
    """二.5：非法 reasoning_effort / reasoning.effort 返回 200 判 violated，并覆盖 Responses 协议。"""
    resp_settings = settings(protocol='openai_responses')
    body = contract._reasoning_effort_invalid(resp_settings, contract.PROBES['reasoning_effort_invalid'])
    assert body['reasoning'] == {'effort': 'invalid_level_xyz'}
    assert 'reasoning_effort' not in body

    assert run_response(
        'reasoning_effort_invalid',
        {'error': {'type': 'invalid_request_error', 'message': 'invalid reasoning.effort'}},
        protocol='openai_responses',
        status=400,
    ).verdict == 'conformed'
    assert run_response(
        'reasoning_effort_invalid',
        answer('openai_responses', text='399'),
        protocol='openai_responses',
        status=200,
    ).verdict == 'violated'


def test_parallel_tools_accepts_dual_calls_and_flags_pseudo_tool_call_text() -> None:
    """三.1：并行多工具要求同轮返回 2 个结构化工具调用，正文伪 tool_call 必须判 violated。"""
    dual_payload = answer('openai', text='')
    dual_payload['choices'][0]['finish_reason'] = 'tool_calls'
    dual_payload['choices'][0]['message'] = {
        'role': 'assistant',
        'content': None,
        'tool_calls': [
            {'id': 'call_1', 'type': 'function', 'function': {'name': 'lookup_ticket', 'arguments': '{"ticket_id":"T-1024"}'}},
            {'id': 'call_2', 'type': 'function', 'function': {'name': 'get_weather', 'arguments': '{"city":"北京"}'}},
        ],
    }
    assert run_response('parallel_tools', dual_payload, protocol='openai').verdict == 'conformed'

    pseudo_payload = answer('openai', text='<tool_call>{"name":"lookup_ticket","arguments":{"ticket_id":"T-1024"}}</tool_call>')
    pseudo_item = run_response('parallel_tools', pseudo_payload, protocol='openai')
    assert pseudo_item.verdict == 'violated'
    assert '伪 tool_call' in pseudo_item.detail


def test_auth_missing_omits_credentials_and_flags_new_api_error() -> None:
    """三.2：无鉴权 401 校验不发鉴权头，401+new_api_error 判 violated，官方 incorrect_api_key_error 判 conformed。"""
    seen_headers: list[dict[str, str]] = []

    def respond_official(request):
        seen_headers.append(dict(request.headers))
        return httpx.Response(401, json={'error': {'type': 'incorrect_api_key_error', 'message': 'Invalid Authentication'}})

    with httpx.Client(transport=httpx.MockTransport(respond_official)) as client:
        ok = contract.run_one(client, settings(protocol='openai'), 'sk-secret', 'auth_missing', 1)
    assert ok.verdict == 'conformed', ok.detail
    assert 'authorization' not in {k.lower() for k in seen_headers[0]}

    def respond_new_api(request):
        return httpx.Response(401, json={'error': {'type': 'new_api_error', 'message': '无效的令牌'}})

    with httpx.Client(transport=httpx.MockTransport(respond_new_api)) as client:
        bad = contract.run_one(client, settings(protocol='openai'), 'sk-secret', 'auth_missing', 1)
    assert bad.verdict == 'violated'
    assert 'new_api_error' in bad.detail


def test_anthropic_cache_invalid_follows_official_200() -> None:
    """Kimi 官方对非法 cache_control ttl/mode 返回 200；200 对齐官方，400 只记观察。"""
    anth_settings = settings(protocol='anthropic')
    body = contract._anthropic_cache_invalid(anth_settings, contract.PROBES['anthropic_cache_invalid'])
    assert body['system'][0]['cache_control']['ttl'] == '999m'
    assert body['system'][0]['cache_control']['mode'] == 'invalid_mode'

    with pytest.raises(protocols.Unsupported):
        contract._anthropic_cache_invalid(settings(protocol='openai'), contract.PROBES['anthropic_cache_invalid'])

    accepted = run_response(
        'anthropic_cache_invalid',
        answer('anthropic', text='OK'),
        protocol='anthropic',
        status=200,
    )
    assert accepted.verdict == 'conformed', accepted.detail
    stricter = run_response(
        'anthropic_cache_invalid',
        {'error': {'type': 'invalid_request_error', 'message': 'invalid cache_control ttl'}},
        protocol='anthropic',
        status=400,
    )
    assert stricter.verdict == 'observation'
    assert '不判违规' in stricter.detail
    bad_type = run_response(
        'anthropic_cache_invalid',
        {'error': {'type': '400001', 'message': 'bad request'}},
        protocol='anthropic',
        status=400,
    )
    assert bad_type.verdict == 'observation'


@pytest.mark.parametrize('protocol', ['openai', 'openai_responses'])
def test_files_roundtrip_uploads_reads_via_file_id_and_deletes(protocol: str) -> None:
    """三.4：/v1/files 上传 → GET 元数据 → 模型通过 file_id 读取通行码 5827 → DELETE 清理。"""
    calls: list[tuple[str, str]] = []

    def respond(request: httpx.Request):
        calls.append((request.method, request.url.path))
        if request.method == 'POST' and request.url.path == '/v1/files':
            return httpx.Response(200, json={'id': 'file_abc123', 'object': 'file', 'filename': 'relay_lab_sign.png'})
        if request.method == 'GET' and request.url.path == '/v1/files/file_abc123':
            return httpx.Response(200, json={'id': 'file_abc123', 'object': 'file', 'status': 'processed'})
        if request.method == 'POST' and request.url.path in ('/v1/chat/completions', '/v1/responses'):
            body = json.loads(request.content)
            if protocol == 'openai':
                assert 'ms://file_abc123' in json.dumps(body)
            else:
                assert 'file_abc123' in json.dumps(body)
            return httpx.Response(200, json=answer(protocol, text='5827'))
        if request.method == 'DELETE' and request.url.path == '/v1/files/file_abc123':
            return httpx.Response(200, json={'id': 'file_abc123', 'deleted': True})
        raise AssertionError(f'Unexpected request: {request.method} {request.url.path}')

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        item = contract.run_one(client, settings(protocol=protocol), 'sk-test', 'files_roundtrip', 1)
    assert item.verdict == 'conformed', item.detail
    assert calls == [
        ('POST', '/v1/files'),
        ('GET', '/v1/files/file_abc123'),
        ('POST', '/v1/chat/completions' if protocol == 'openai' else '/v1/responses'),
        ('DELETE', '/v1/files/file_abc123'),
    ]


def test_suite_decouples_global_audits_from_business_checks_with_exact_denominators(monkeypatch) -> None:
    """解耦验收：单条业务用例不被全局字段缺失连坐，专项汇总项输出精确分母并统一扣分。"""
    real_client = httpx.Client

    def respond(request: httpx.Request):
        body = json.loads(request.content)
        # tool_choice=required / none 返回带腾讯云 MaaS 数字 code + 空 param + 兜底文案的 400
        if body.get('tool_choice') in ('required', 'none'):
            return httpx.Response(
                400,
                json={'error': {'type': 'invalid_request_error', 'code': '400001', 'param': '', 'message': 'Invalid request'}},
            )
        # 正常 200 故意缺失 previous_response_id / conversation 与 cache_write_tokens
        payload = answer('openai_responses', text='SYSTEMOK')
        del payload['previous_response_id']
        del payload['conversation']
        payload['usage'].pop('input_tokens_details', None)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(contract.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(respond)))
    suite_settings = settings(
        protocol='openai_responses',
        protocols=['openai_responses'],
        checks=[
            'system',
            'tool_choice_required',
            'tool_choice_none',
            'response_shape',
            'usage',
            'cache_write_headers_audit',
            'error_fields_audit',
        ],
    )
    items = contract.run_suite(suite_settings, 'sk-test')
    by_check = {item.check: item for item in items}

    # 1) 业务用例本身全部通过，不被连坐
    assert by_check['system'].verdict == 'conformed'
    assert by_check['tool_choice_required'].verdict == 'conformed'
    assert by_check['tool_choice_none'].verdict == 'conformed'

    # 2) response_shape / usage / cache_write_headers_audit 汇总精确分母（system 2 次 + shape 1 + usage 1 = 4）
    assert by_check['response_shape'].verdict == 'violated'
    assert by_check['response_shape'].severity == 'high'
    assert 'previous_response_id 缺失/非 null 4/4' in by_check['response_shape'].detail
    assert 'conversation 缺失/非 null 4/4' in by_check['response_shape'].detail

    assert by_check['usage'].verdict == 'violated'
    assert by_check['usage'].severity == 'high'
    assert 'usage.input_tokens_details.cache_write_tokens 缺失/非法 4/4' in by_check['usage'].detail

    assert by_check['cache_write_headers_audit'].verdict == 'violated'
    assert by_check['cache_write_headers_audit'].severity == 'high'
    assert by_check['cache_write_headers_audit'].requests == 0
    assert '正写档不明 4/4' in by_check['cache_write_headers_audit'].detail

    # 3) error_fields_audit 0 额外请求汇总全量 4xx 机器字段共性问题
    assert by_check['error_fields_audit'].verdict == 'observation'
    assert by_check['error_fields_audit'].requests == 0
    assert '400001' in by_check['error_fields_audit'].detail
    assert '2/2 (100.0%)' in by_check['error_fields_audit'].detail
    assert 'param=""' in by_check['error_fields_audit'].detail


def test_multi_round_probe_records_and_renders_per_round_response_headers(monkeypatch) -> None:
    """多轮用例逐轮记录响应头并在报告证据块中按轮次输出 5m/1h 写档头与 x-request-id。"""
    monkeypatch.setattr(contract, '_wait_cache', lambda seconds, stop: True)
    call_no = 0

    def respond(request: httpx.Request):
        nonlocal call_no
        call_no += 1
        data = answer('openai', text='OK')
        if call_no == 1:
            data['usage'].update(
                cache_creation_input_tokens=512,
                cache_read_input_tokens=0,
                prompt_tokens_details={'cache_write_tokens': 512, 'cached_tokens': 0},
            )
            headers = {
                'Msh-Usage-Cache-Write-Tokens-5m': '0',
                'Msh-Usage-Cache-Write-Tokens-1h': '512',
                'x-request-id': 'req-round-1',
                'x-oneapi-request-id': 'oneapi-round-1',
            }
        else:
            data['usage'].update(
                cache_creation_input_tokens=0,
                cache_read_input_tokens=512,
                prompt_tokens_details={'cache_write_tokens': 0, 'cached_tokens': 512},
            )
            headers = {
                'Msh-Usage-Cache-Write-Tokens-5m': '0',
                'Msh-Usage-Cache-Write-Tokens-1h': '0',
                'x-request-id': 'req-round-2',
                'x-oneapi-request-id': 'oneapi-round-2',
            }
        return httpx.Response(200, json=data, headers=headers)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        item = contract.run_one(client, settings(protocol='openai'), 'sk-test', 'cache_roundtrip', 1)
    assert item.verdict == 'conformed', item.detail
    assert len(item.round_response_headers) == 2
    assert item.round_response_headers[0]['msh-usage-cache-write-tokens-1h'] == '512'
    assert item.round_response_headers[0]['x-request-id'] == 'req-round-1'
    assert item.round_response_headers[1]['x-request-id'] == 'req-round-2'

    lines: list[str] = []
    contract._append_wire_evidence(lines, item)
    rendered = '\n'.join(lines)
    assert '**响应体 · 第 1 轮**' in rendered
    assert 'msh-usage-cache-write-tokens-1h=512' in rendered
    assert 'x-request-id=req-round-1' in rendered
    assert '**响应体 · 第 2 轮**' in rendered
    assert 'x-request-id=req-round-2' in rendered


def test_audit_splits_cache_dimensions_and_excludes_files_from_inference_4xx() -> None:
    """汇总项要能点名序号/轮次/Request ID，并把 /v1/files 与推理 4xx 分开。"""
    chat = settings()
    audit = contract._ProtocolAudit()
    audit.record_reply(
        'openai', chat,
        protocols.Reply(status=200, headers={'x-request-id': 'req-short'}),
        index=3, check='system', round_no=1,
    )
    audit.record_reply(
        'openai', chat,
        protocols.Reply(
            status=200,
            usage=protocols.Usage(prompt_tokens=100, cache_write_tokens=800),
            headers={'x-request-id': 'req-implicit'},
        ),
        index=40, check='cache', round_no=1,
    )
    audit.record_reply(
        'openai', chat,
        protocols.Reply(
            status=200,
            usage=protocols.Usage(prompt_tokens=100, cache_write_tokens=640),
            headers={
                'x-request-id': 'req-explicit',
                'msh-usage-cache-write-tokens-5m': '640',
                'msh-usage-cache-write-tokens-1h': '0',
            },
        ),
        index=42, check='cache_headers_5m', round_no=2,
    )
    audit.record_reply(
        'openai', chat,
        protocols.Reply(status=401, error_type='new_api_error', error='无效的令牌'),
        index=49, check='files_roundtrip', round_no=1, endpoint='files',
    )
    audit.record_reply(
        'openai', chat,
        protocols.Reply(
            status=400,
            error_type='invalid_request_error',
            error_message='tools cannot be used with content',
        ),
        index=11, check='dynamic_tools_mutex', round_no=1,
    )

    (verdict, _, detail, _), _missing = contract._summarize_cache_write_headers_audit(audit)
    assert verdict == 'violated'
    assert '显式 5m/1h 对齐' in detail
    assert '长前缀隐式' in detail and '缺头' in detail
    assert '#40 cache 第1轮 req-implicit' in detail
    assert '短请求' in detail
    assert '#3 system 第1轮 req-short' in detail
    billing = contract._cache_dimension_text(audit, kind='billing')
    assert '显式 5m/1h 计费字段对齐' in billing
    assert '短请求 缺失 1/1' in billing

    err_verdict, _, err_detail, _ = contract._summarize_error_fields_audit(audit)
    assert err_verdict == 'conformed', err_detail
    assert '独立端点 /v1/files' in err_detail
    assert '#49 files_roundtrip 第1轮 无' in err_detail
    assert 'new_api_error' in err_detail
    assert audit.bad_401_type == 0
    assert audit.total_4xx == 1


def test_stability_retry_marks_flake_as_observation_and_repeat_as_violated() -> None:
    """模型偶发未按指令输出时重试 1 次：第二次通过记观察，连续两次才判稳定不通过。"""
    calls = {'n': 0}

    def flake(request):
        calls['n'] += 1
        text = '{"ok": false}' if calls['n'] == 1 else '{"ok": true}'
        return httpx.Response(200, json=answer('openai', text))

    with httpx.Client(transport=httpx.MockTransport(flake)) as client:
        flaky = contract.run_one(client, settings(), None, 'json_object', 1)
    assert flaky.verdict == 'observation', flaky.detail
    assert flaky.requests == 2
    assert '重试通过' in flaky.detail

    def stable_fail(request):
        return httpx.Response(200, json=answer('openai', '{"ok": false}'))

    with httpx.Client(transport=httpx.MockTransport(stable_fail)) as client:
        failed = contract.run_one(client, settings(), None, 'json_object', 1)
    assert failed.verdict == 'violated'
    assert failed.requests == 2
    assert '连续 2 次复现' in failed.detail







