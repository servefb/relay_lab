from __future__ import annotations

import json

import anyio
import httpx2
import pytest
from fastapi.testclient import TestClient

from relay_lab import ai_client, store
from relay_lab.ai_api import AIStream
from relay_lab.ai_diagnose import summarize_run_for_ai
from relay_lab.api import create_app
from relay_lab.config import ContractItem, LabFinding, LabRun, LabStage, QualityItem
from tests.conftest import login

KEY = 'test-unmistakable-secret-1234'


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.setenv('RELAY_LAB_AI_URL', 'https://relay.example/v1')
    monkeypatch.setenv('RELAY_LAB_AI_MODEL', 'test-model')
    monkeypatch.setenv('RELAY_LAB_AI_API_KEY', KEY)
    monkeypatch.setenv('RELAY_LAB_AI_PROTOCOL', 'openai')
    calls, replies = [], []

    def handler(request):
        assert request.headers['authorization'] == f'Bearer {KEY}'
        assert request.url.host == 'relay.example'
        assert KEY not in request.content.decode()
        calls.append((request.url.path, json.loads(request.content)))
        assert replies, 'Unexpected SDK request'
        reply = replies.pop(0)
        if isinstance(reply, httpx2.Response):
            return reply
        return httpx2.Response(200, content=reply, headers={'content-type': 'text/event-stream'})

    monkeypatch.setattr(ai_client, '_transport', lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return calls, replies


def _run(i=0, kind='speed', status='completed', **data):
    return LabRun(run_id=f'relay-test-{i}', created_at=f'2026-01-01T00:00:{i:02}Z', kind=kind, status=status,
                  settings={'model': 'tested', 'url': 'https://tested.example/private/path'}, fingerprint='test', **data)


def _events(response):
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('text/event-stream')
    return [(a.removeprefix('event: '), json.loads(b.removeprefix('data: ')))
            for a, b in (block.splitlines() for block in response.text.strip().split('\n\n'))]


def _chat_stream(*choices):
    chunks = [{'id': 'chatcmpl-test', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'test-model',
               'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]} for delta, finish in choices]
    return ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'


def _chat_text(text):
    return _chat_stream(({'role': 'assistant', 'content': text}, None), ({}, 'stop'))


def _response_stream(text=None, call=None):
    if call is not None:
        tool_id, name, arguments = call
        output = {'type': 'function_call', 'id': 'fc_' + tool_id, 'call_id': tool_id, 'name': name,
                  'arguments': json.dumps(arguments), 'status': 'completed'}
        events = [('response.output_item.added', {'output_index': 0, 'item': {**output, 'arguments': ''}})]
    else:
        content = {'type': 'output_text', 'text': text, 'annotations': []}
        output = {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed', 'content': [content]}
        events = [
            ('response.output_item.added', {'output_index': 0, 'item': {**output, 'content': []}}),
            ('response.output_text.delta', {'output_index': 0, 'content_index': 0, 'item_id': 'msg_1',
                                            'delta': text, 'logprobs': []}),
        ]
    response = {'id': 'resp_mock', 'object': 'response', 'created_at': 1, 'model': 'test-model', 'status': 'completed',
                'output': [output], 'parallel_tool_calls': False, 'tool_choice': 'auto', 'tools': []}
    events = [('response.created', {'response': {**response, 'status': 'in_progress', 'output': []}}), *events,
              ('response.output_item.done', {'output_index': 0, 'item': output}),
              ('response.completed', {'response': response})]
    return ''.join('data: ' + json.dumps(dict(fields, type=name, sequence_number=i)) + '\n\n'
                   for i, (name, fields) in enumerate(events))


def test_config_and_probe(tmp_path, sdk):
    calls, replies = sdk
    with TestClient(create_app(tmp_path, schedule_loop=False)) as client:
        assert client.get('/api/ai/config').status_code == 401
        login(client)
        assert client.get('/api/ai/config', headers={'Origin': 'https://external.example'}).status_code == 403
        assert client.get('/api/openapi.json').json()['paths']['/api/ai/chat']['post']['responses']['200']
        response = client.get('/api/ai/config')
        assert response.json()['configured'] and KEY not in response.text
        assert client.put('/api/ai/config', json={'model': 'replacement'}).json()['has_key']
        # Authenticated PUT may approve a new origin; changing the base URL clears the key.
        switched = client.put('/api/ai/config', json={'url': 'http://other.example/v1'})
        assert switched.status_code == 200
        assert not switched.json()['has_key']
        assert client.put('/api/ai/config', json={
            'url': 'https://relay.example/v1', 'model': 'replacement', 'api_key': KEY,
        }).json()['has_key']
        replies.append(httpx2.Response(429, json={'error': {'message': KEY}}))
        result = client.post('/api/ai/test')
        assert (result.status_code, result.json()['code']) == (429, 'upstream_rate_limit')
        assert KEY not in result.text
        assert len(calls) == 1 and calls[0][1]['model'] == 'replacement'
        assert not client.put('/api/ai/config', json={'url': 'https://api.openai.com/v1'}).json()['has_key']
        assert client.post('/api/ai/test').status_code == 409
        assert len(calls) == 1


def test_diagnosis_cache_and_broadcast(tmp_path, sdk):
    run = _run()
    store.save(tmp_path, run)
    calls, replies = sdk
    replies.append(_chat_stream(({'content': 'first '}, None), ({'content': KEY[:12]}, None),
                               ({'content': KEY[12:]}, None), ({}, 'stop')))
    with TestClient(create_app(tmp_path, schedule_loop=False)) as client:
        login(client)
        with client.websocket_connect('/api/ws') as socket:
            assert socket.receive_json()['type'] == 'snapshot'
            route = f'/api/runs/{run.run_id}/analyze'
            response = client.post(route)
            events = _events(response)
            assert KEY not in response.text
            assert events[-1][0] == 'done' and events[-1][1]['persisted']
            result = socket.receive_json()['run']['ai_analysis']
            assert result['summary_md'] == 'first [已脱敏]'
            assert client.get(f'/api/runs/{run.run_id}').json()['ai_analysis'] == result
            assert _events(client.post(route))[-1][1]['cached']
            assert len(calls) == 1
            replies.append(_chat_stream(({'content': 'partial'}, None)))
            assert _events(client.post(route, json={'force': True}))[-1][0] == 'error'
            replies.append(_chat_text('follow up'))
            assert not _events(client.post(route, json={'focus': 'latency'}))[-1][1]['persisted']
            assert store.load_run(tmp_path, run.run_id).ai_analysis.summary_md == result['summary_md']


@pytest.mark.parametrize('kind', ['capacity', 'speed', 'parallel', 'load', 'quality', 'contract'])
def test_summary_only_uses_report_evidence(kind):
    run = _run(kind=kind, message='Authorization: Bearer hidden-credential', stages=[
        LabStage(index=1, phase='rpm', pressure=1, repeat=1, native_task_id='internal-path'),
    ], findings=[LabFinding(phase='rpm', status='lower_bound', value=90)], contract=[
        ContractItem(index=1, check='system', title='System', verdict='inconclusive', severity='high',
                     request_body='raw-request', response_body='raw-response'),
    ], items=[QualityItem(index=i, category='example', prompt='raw-question', expected='A') for i in range(20)])
    summary = summarize_run_for_ai(run)
    text = json.dumps(summary)
    assert not any(marker in text for marker in (
        'hidden-credential', 'internal-path', 'raw-request', 'raw-response', 'raw-question', 'private/path',
    ))
    assert all('ttft_p99_ms' not in stage for stage in summary.get('stages', []))
    if kind == 'capacity':
        assert summary['findings'][0]['status'] == 'lower_bound'
    if kind == 'contract':
        assert summary['contract_findings'][0]['verdict'] == 'inconclusive'
    if kind == 'quality':
        assert summary['failed_items_omitted'] == 5


@pytest.mark.parametrize('protocol', ['openai', 'openai_responses'])
def test_agents_sdk_tool_roundtrip(tmp_path, sdk, protocol):
    calls, replies = sdk
    args = {'kind': 'load', 'settings': {'concurrency_start': 5}, 'rationale': 'Conservative starting point'}
    if protocol == 'openai':
        raw = json.dumps(args)
        replies.extend([_chat_stream(
            ({'tool_calls': [{'index': 0, 'id': 'call_1', 'type': 'function', 'function': {
                'name': 'propose_experiment_config', 'arguments': raw[:20],
            }}]}, None),
            ({'tool_calls': [{'index': 0, 'function': {'arguments': raw[20:]}}]}, None),
            ({}, 'tool_calls'),
        ), _chat_text('Draft ready.')])
    else:
        replies.extend([_response_stream(call=('call_1', 'propose_experiment_config', args)),
                        _response_stream('Draft ready.')])
    with TestClient(create_app(tmp_path, schedule_loop=False)) as client:
        login(client)
        assert client.put('/api/ai/config', json={'protocol': protocol}).status_code == 200
        response = client.post('/api/ai/chat', json={'messages': [{'role': 'user', 'content': 'Recommend load settings'}]})
        events = _events(response)
        names = [event[0] for event in events]
        assert names == ['tool_start', 'tool_end', 'config_draft', 'delta', 'done'], events
        assert events[2][1]['settings'] == {'concurrency_start': 5}
        assert client.get('/api/runs').json()['runs'] == []
        assert len(calls) == 2 and not replies
        assert len(calls[0][1]['tools']) == 5
        assert 'call_1' in json.dumps(calls[1][1])
        if protocol == 'openai_responses':
            assert all(path == '/v1/responses' and not body['store'] for path, body in calls)
            assert calls[1][1]['input'][-1]['type'] == 'function_call_output'
        else:
            assert all(path == '/v1/chat/completions' for path, _ in calls)
            assert calls[1][1]['messages'][-1]['role'] == 'tool'


def test_agent_can_correct_invalid_draft_without_granting_authorization(tmp_path, sdk):
    calls, replies = sdk
    invalid = {'kind': 'load', 'settings': {'concurrency_start': 5, 'authorized': True}, 'rationale': 'Test'}
    corrected = {'kind': 'load', 'settings': {'concurrency_start': 5}, 'rationale': 'Test'}
    replies.extend([
        _response_stream(call=('bad_1', 'propose_experiment_config', invalid)),
        _response_stream(call=('good_2', 'propose_experiment_config', corrected)),
        _response_stream('Corrected.'),
    ])
    with TestClient(create_app(tmp_path, schedule_loop=False)) as client:
        login(client)
        client.put('/api/ai/config', json={'protocol': 'openai_responses'}).raise_for_status()
        events = _events(client.post('/api/ai/chat', json={'messages': [{'role': 'user', 'content': 'Draft'}]}))
        assert [data['ok'] for name, data in events if name == 'tool_end'] == [False, True]
        assert [data['settings'] for name, data in events if name == 'config_draft'] == [{'concurrency_start': 5}]
        assert events[-1][0] == 'done'
        assert 'only_experiment_parameters_allowed' in calls[1][1]['input'][-1]['output']
        assert client.get('/api/runs').json()['runs'] == []


def test_responses_diagnosis_loads_old_report(tmp_path, sdk):
    run = _run(0)
    store.save(tmp_path, run)
    for i in range(1, 52):
        store.save(tmp_path, _run(i))
    calls, replies = sdk
    replies.append(_response_stream('Limited evidence.'))
    with TestClient(create_app(tmp_path, schedule_loop=False)) as client:
        login(client)
        client.put('/api/ai/config', json={'protocol': 'openai_responses'}).raise_for_status()
        assert run.run_id not in {item['run_id'] for item in client.get('/api/runs').json()['runs']}
        route = f'/api/runs/{run.run_id}/analyze'
        assert _events(client.post(route))[-1][1]['persisted']
        assert calls[0][0] == '/v1/responses' and calls[0][1]['store'] is False
    with TestClient(create_app(tmp_path, schedule_loop=False)) as restarted:
        login(restarted)
        assert _events(restarted.post(route))[-1][1]['cached']
        assert len(calls) == 1


def test_stream_cancels_asgi_24_before_next_chunk():
    async def run():
        started, closed = anyio.Event(), anyio.Event()

        async def producer(emit):
            started.set()
            try:
                await anyio.sleep_forever()
            finally:
                closed.set()

        async def receive():
            await started.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            assert message['type'] == 'http.response.start'

        with anyio.fail_after(2):
            await AIStream(producer)({'asgi': {'spec_version': '2.4'}, 'type': 'http'}, receive, send)
        assert closed.is_set()

    anyio.run(run)
