"""Behaviour tests for the relay-fidelity suite, the protocol adapters and the metrics.

No network and no credentials: the relay is a local fixture that can be told to be
faithful or to tamper, which is the only way to prove the suite detects rewriting
*without* also accusing an honest site — the failure mode that would make these verdicts
worthless.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from relay_lab import contract, protocols, window
from relay_lab.config import ContractItem, ContractRequest, ContractSettings

LONG_ANSWER = ' '.join(f'word{index}' for index in range(400))


class _Relay(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *, faithful: bool) -> None:
        super().__init__(('127.0.0.1', 0), _Handler)
        self.faithful = faithful
        self.seen: list[dict] = []


class _Handler(BaseHTTPRequestHandler):
    server: _Relay

    def log_message(self, *args) -> None:
        """Keep test output clean."""

    def do_POST(self) -> None:
        length = int(self.headers.get('Content-Length', '0'))
        body = json.loads(self.rfile.read(length) or b'{}')
        self.server.seen.append(body)
        faithful = self.server.faithful
        protocol = self._protocol()

        if body.get('model') == contract.INVALID_MODEL:
            self._json(404, {'error': {'type': 'resource_not_found_error', 'message': 'model not found'}})
            return

        system, history, prompt = self._prompt_parts(body, protocol)
        tools = body.get('tools')
        schema = bool(body.get('response_format') or (body.get('text') or {}).get('format'))
        stop = body.get('stop') or body.get('stop_sequences')
        cap = body.get('max_completion_tokens') or body.get('max_tokens') or body.get('max_output_tokens')
        if not faithful:
            tools, schema, stop, cap = None, False, None, None

        text = self._answer(prompt, system, history, schema, faithful)
        if faithful and stop:
            for word in stop:
                if word and word in text:
                    text = text.split(word, 1)[0]
        if faithful and cap and cap < 200:
            text = ' '.join(text.split()[:cap])

        completion = max(1, len(text) // 2)
        if faithful and cap:
            completion = min(completion, cap)
        usage = self._usage(protocol, completion) if faithful else {}
        want_tool = bool(tools and ('工单' in prompt or 'T-1024' in prompt))
        if body.get('stream'):
            self._stream(protocol, text, faithful, usage)
        else:
            self._single(protocol, text, usage, want_tool)

    def _protocol(self) -> str:
        path = self.path.split('?', 1)[0]
        if path.endswith('/responses'):
            return 'openai_responses'
        if path.endswith('/messages'):
            return 'anthropic'
        return 'openai'

    def _prompt_parts(self, body: dict, protocol: str) -> tuple[str, list, str]:
        if protocol == 'openai_responses':
            system = body.get('instructions') or ''
            history = body.get('input') or []
            last = history[-1] if history else {}
            content = last.get('content') if isinstance(last, dict) else last
            if isinstance(content, list):
                prompt = ' '.join(
                    part.get('text', '') for part in content if isinstance(part, dict) and part.get('text')
                )
            else:
                prompt = content if isinstance(content, str) else json.dumps(content or '', ensure_ascii=False)
            return system, history, prompt or ''

        messages = body.get('messages') or []
        system = body.get('system') or ''
        if not system:
            system = next((m.get('content') or '' for m in messages if m.get('role') == 'system'), '')
        if isinstance(system, list):
            system = ' '.join(
                part.get('text', '') for part in system if isinstance(part, dict) and part.get('text')
            )
        history = [m for m in messages if m.get('role') != 'system']
        last = history[-1].get('content') if history else ''
        prompt = last if isinstance(last, str) else json.dumps(last, ensure_ascii=False)
        return system, history, prompt or ''

    def _usage(self, protocol: str, completion: int) -> dict:
        if protocol == 'openai':
            return {'prompt_tokens': 20, 'completion_tokens': completion, 'total_tokens': 20 + completion}
        if protocol == 'openai_responses':
            return {'input_tokens': 20, 'output_tokens': completion, 'total_tokens': 20 + completion}
        return {'input_tokens': 20, 'output_tokens': completion}

    def _answer(self, prompt, system, history, schema, faithful) -> str:
        if system and contract.SYSTEM_MARKER in system:
            return contract.SYSTEM_MARKER
        if contract.USER_MARKER in prompt:
            return contract.USER_MARKER
        if '481516' in str(history):
            return '481516'
        if '数到 30' in prompt or '从 1 数到' in prompt:
            return '\n'.join(str(n) for n in range(1, 31))
        if schema:
            return '{"model": "X1", "price": 1299, "in_stock": true}'
        if '山间清晨' in prompt or '河流' in prompt:
            return LONG_ANSWER
        return '好的。'

    def _single(self, protocol: str, text: str, usage: dict, want_tool: bool) -> None:
        if want_tool:
            if protocol == 'openai':
                payload = {
                    'choices': [{
                        'message': {
                            'content': None,
                            'tool_calls': [{
                                'id': 'c1',
                                'type': 'function',
                                'function': {'name': contract.TOOL_NAME, 'arguments': '{"ticket_id":"T-1024"}'},
                            }],
                        },
                        'finish_reason': 'tool_calls',
                    }],
                    'usage': usage,
                }
            elif protocol == 'openai_responses':
                payload = {
                    'id': 'resp_1', 'object': 'response', 'status': 'completed',
                    'output': [{
                        'id': 'fc_1', 'type': 'function_call', 'call_id': 'c1',
                        'name': contract.TOOL_NAME, 'arguments': '{"ticket_id":"T-1024"}',
                    }],
                    'usage': usage,
                }
            else:
                payload = {
                    'id': 'msg_1', 'type': 'message', 'role': 'assistant',
                    'content': [{'type': 'tool_use', 'id': 'c1', 'name': contract.TOOL_NAME,
                                 'input': {'ticket_id': 'T-1024'}}],
                    'stop_reason': 'tool_use', 'usage': usage,
                }
        elif protocol == 'openai':
            payload = {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}], 'usage': usage}
        elif protocol == 'openai_responses':
            payload = {
                'id': 'resp_1', 'object': 'response', 'status': 'completed',
                'output': [{'id': 'msg_1', 'type': 'message', 'role': 'assistant',
                            'content': [{'type': 'output_text', 'text': text}]}],
                'usage': usage,
            }
        else:
            payload = {
                'id': 'msg_1', 'type': 'message', 'role': 'assistant',
                'content': [{'type': 'text', 'text': text}],
                'stop_reason': 'end_turn', 'usage': usage,
            }
        self._json(200, payload)

    def _stream(self, protocol: str, text: str, faithful: bool, usage: dict) -> None:
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        words = text.split(' ') if faithful else [text]
        if not faithful:
            time.sleep(0.12)
        if protocol == 'openai':
            for word in words:
                self._event({'choices': [{'delta': {'content': word + (' ' if faithful else '')}}]})
                if faithful:
                    time.sleep(0.008)
            if not faithful:
                self._event({'choices': [{'delta': {}}], 'usage': usage})
            self.wfile.write(b'data: [DONE]\n\n')
        elif protocol == 'openai_responses':
            for word in words:
                self._event({'type': 'response.output_text.delta', 'delta': word + (' ' if faithful else '')})
                if faithful:
                    time.sleep(0.008)
            self._event({'type': 'response.completed', 'response': {'status': 'completed', 'usage': usage}})
        else:
            for word in words:
                self._event({
                    'type': 'content_block_delta', 'index': 0,
                    'delta': {'type': 'text_delta', 'text': word + (' ' if faithful else '')},
                })
                if faithful:
                    time.sleep(0.008)
            self._event({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': usage})
            self._event({'type': 'message_stop'})
        self.wfile.flush()

    def _event(self, value: dict) -> None:
        self.wfile.write(('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode())
        self.wfile.flush()

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def honest():
    server = _Relay(faithful=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture()
def tampering():
    server = _Relay(faithful=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def settings_for(server: _Relay, checks: list[str]) -> ContractSettings:
    return ContractSettings(
        url=f'http://127.0.0.1:{server.server_address[1]}',
        model='sim',
        checks=checks,
        request_timeout=20,
        cache_prefix_tokens=512,
    )


def verdicts(items: list[ContractItem], protocol: str = 'openai') -> dict[str, str]:
    """Index by check for one wire; the suite always covers three protocols."""
    return {item.check: item.verdict for item in items if item.protocol == protocol}


CHECKS = ['system', 'tools', 'json_schema', 'max_tokens', 'stop', 'multi_turn', 'errors', 'stream']


# ---------- the suite must detect tampering ----------


def test_tampering_relay_is_flagged_on_every_high_severity_check(tampering) -> None:
    got = verdicts(contract.run_suite(settings_for(tampering, CHECKS), None))
    for check in ('tools', 'json_schema', 'max_tokens', 'stop', 'stream'):
        assert got[check] == 'violated', f'{check} was not flagged: {got[check]}'


def test_tampering_relay_keeps_a_honest_404_as_conformed(tampering) -> None:
    """The error probe's subject *is* the failure, so a real 404 must count as a pass."""
    summary = contract.summarize(contract.run_suite(settings_for(tampering, ['errors']), None))
    for protocol in ('openai', 'openai_responses', 'anthropic'):
        score = summary.by_check[f'{protocol}/errors']
        assert score.conformed == 1
        assert score.violated == 0


def test_model_not_found_mapped_to_503_is_flagged_as_a_contract_violation() -> None:
    """A client model error must not be exposed as a retryable 5xx."""
    settings = ContractSettings(url='http://127.0.0.1:1', model='m', checks=['errors'])
    reply = protocols.Reply(status=503, error='HTTP 503：No available channel', error_code='model_not_found')
    verdict, _, _, _ = contract._judge_errors(settings, reply, contract.PROBES['errors'])
    assert verdict == 'violated'


@pytest.mark.parametrize(
    ('status', 'error_type', 'error_code', 'message', 'expected'),
    [
        # 官方错误码说明: 400 → invalid_request_error, 404 → resource_not_found_error.
        (404, 'resource_not_found_error', None, 'The requested model does not exist', 'conformed'),
        (400, 'invalid_request_error', None, 'The requested model does not exist', 'conformed'),
        (404, 'invalid_request_error', None, 'The requested model does not exist', 'violated'),
        (400, 'resource_not_found_error', None, 'The requested model does not exist', 'violated'),
        # A code-only body (new-api style) is a deviation: official bodies carry error.type.
        (404, None, 'model_not_found', 'The model does not exist', 'violated'),
        (404, 'new_api_error', None, 'The requested model does not exist', 'violated'),
        (404, None, None, 'The requested model does not exist', 'violated'),
        (404, None, None, '', 'violated'),
    ],
)
def test_model_error_type_controls_report_verdict_and_summary(
    status, error_type, error_code, message, expected,
) -> None:
    settings = ContractSettings(url='https://site.test', model='m', checks=['errors'])
    error: dict = {'message': message}
    if error_type is not None:
        error['type'] = error_type
    if error_code is not None:
        error['code'] = error_code
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json={'error': error}))) as client:
        item = contract.run_one(client, settings, None, 'errors', 1)
    assert item.verdict == expected
    assert f'type={error_type or "缺失"}' in item.observed
    # The official per-status error.type stays a hard requirement in the report text.
    assert 'resource_not_found_error' in item.expected
    summary = contract.summarize([item])
    assert summary.violated == int(expected == 'violated')
    assert summary.conformed == int(expected == 'conformed')


def test_auth_and_rate_limit_errors_are_not_model_error_passes() -> None:
    settings = ContractSettings(url='http://127.0.0.1:1', model='m', checks=['errors'])
    for status, error in ((401, 'invalid_api_key'), (403, 'forbidden'), (429, 'rate_limit_exceeded')):
        reply = protocols.Reply(status=status, error=f'HTTP {status}: {error}')
        verdict, _, _, _ = contract._judge_errors(settings, reply, contract.PROBES['errors'])
        assert verdict == 'inconclusive'


def test_kimi_errors_requires_official_error_type_while_glm_keeps_code_message() -> None:
    """Moonshot 官方 errors.md 要求 404 返回 error.type=resource_not_found_error；GLM 沿用 code_message。"""
    kimi_k3 = ContractSettings(url='http://127.0.0.1:1', model='kimi-k3.1-pro', checks=['errors'])
    kimi_k2 = ContractSettings(url='http://127.0.0.1:1', model='kimi-k2.5', checks=['errors'])
    glm = ContractSettings(url='http://127.0.0.1:1', model='glm-5.3', checks=['errors'])

    official_404 = protocols.Reply(
        status=404, error='HTTP 404：model not found', error_type='resource_not_found_error',
    )
    new_api_404 = protocols.Reply(
        status=404, error='HTTP 404：model not found', error_type='new_api_error', error_code='model_not_found',
    )
    assert contract._judge_errors(kimi_k3, official_404, contract.PROBES['errors'])[0] == 'conformed'
    assert contract._judge_errors(kimi_k2, official_404, contract.PROBES['errors'])[0] == 'conformed'
    assert contract._judge_errors(kimi_k3, new_api_404, contract.PROBES['errors'])[0] == 'violated'
    assert contract._judge_errors(kimi_k2, new_api_404, contract.PROBES['errors'])[0] == 'violated'
    assert contract._judge_errors(glm, new_api_404, contract.PROBES['errors'])[0] == 'conformed'


def test_run_one_persists_request_and_response_bodies_for_export() -> None:
    """Export reports need the wire request/response, not only expected/observed prose."""
    settings = ContractSettings(url='https://site.test/v1', model='demo', checks=['response_shape'], protocol='openai')
    payload = {
        'id': 'chatcmpl-1',
        'object': 'chat.completion',
        'created': 1_700_000_000,
        'model': 'demo',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 1, 'total_tokens': 4},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == 'POST'
        body = json.loads(request.content.decode('utf-8'))
        assert body['model'] == 'demo'
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        item = contract.run_one(client, settings, 'sk-test', 'response_shape', 1)

    assert item.verdict == 'conformed'
    assert '"model": "demo"' in item.request_body
    assert 'sk-test' not in item.request_body
    assert item.request_headers.get('Authorization') == '***'
    assert item.request_headers.get('Content-Type') == 'application/json'
    assert item.response_body.startswith('HTTP 200')
    assert '"chat.completion"' in item.response_body
    assert 'OK' in item.response_body
    assert item.missing_response_headers == []


def test_request_cost_accounts_for_multi_request_checks() -> None:
    assert contract.request_cost('system') == 2
    assert contract.request_cost('cache') == 2
    assert contract.request_cost('routing') == 3


def test_dynamic_tools_capability_rejection_is_not_always_a_violation() -> None:
    """Non-Kimi models may lack system-tools; a plain 400 stays inconclusive."""
    settings = ContractSettings(url='http://127.0.0.1:1', model='gpt-test', protocol='openai', checks=['dynamic_tools'])
    reply = protocols.Reply(status=400, error='HTTP 400: tools unsupported', error_type='invalid_request_error')
    assert contract._judge_dynamic_tools(settings, reply, contract.PROBES['dynamic_tools'])[0] == 'inconclusive'


def test_openai_tools_probe_omits_tool_choice_like_postman() -> None:
    """TokenHub Postman 02 relies on the prompt; named tool_choice 400s on kimi."""
    settings = ContractSettings(url='http://127.0.0.1:1', model='kimi-k3.1-pro', protocol='openai', checks=['tools'])
    body = contract._tools(settings, contract.PROBES['tools'])
    assert 'tool_choice' not in body
    assert body.get('max_completion_tokens') == 2048
    assert body.get('reasoning_effort') == 'low'


def test_model_profile_auto_matches_kimi_and_glm() -> None:
    from relay_lab.model_profiles import resolve_profile

    kimi = resolve_profile('kimi-k3.1-pro')
    glm = resolve_profile('glm-5.3')
    generic = resolve_profile('gpt-4o-mini')
    assert kimi.family == 'kimi' and kimi.supports_dynamic_tools
    assert kimi.default_temperature == 1.0
    assert kimi.allows_thinking_disable is False
    k25 = resolve_profile('kimi-k2.5')
    assert k25.family == 'kimi' and k25.allows_thinking_disable is True
    assert k25.default_temperature == 1.0
    assert glm.family == 'glm' and not glm.supports_dynamic_tools
    assert 'dynamic_tools' in kimi.default_checks
    assert 'dynamic_tools_role' in kimi.default_checks
    assert 'dynamic_tools_dup' in kimi.default_checks
    assert 'tool_choice_required_bare' in kimi.default_checks
    assert 'dynamic_tools' not in glm.default_checks
    assert 'tool_choice_required_bare' in glm.default_checks
    assert 'tool_choice_required_bare' in generic.default_checks
    assert 'preserved_thinking' in glm.default_checks
    # A3: Msh cache headers / prompt_cache_options are Kimi-only — never in GLM/generic packs.
    assert 'cache_headers' in kimi.default_checks
    assert 'cache_options_echo' in kimi.default_checks
    assert 'cache_headers' not in glm.default_checks
    assert 'cache_options_echo' not in glm.default_checks
    assert 'cache_headers' not in generic.default_checks

    kimi_settings = ContractSettings(url='http://127.0.0.1:1', model='kimi-k3.1-pro', checks=[])
    glm_settings = ContractSettings(url='http://127.0.0.1:1', model='glm-5.3', checks=[])
    assert 'dynamic_tools' in contract.select(kimi_settings)
    assert 'dynamic_tools' not in contract.select(glm_settings)
    assert 'cache_headers' not in contract.select(glm_settings)

    glm_tools = contract._tools(
        ContractSettings(url='http://127.0.0.1:1', model='glm-5.3', protocol='openai', checks=['tools']),
        contract.PROBES['tools'],
    )
    assert glm_tools.get('tool_choice') == 'auto'
    assert glm_tools.get('max_tokens') == 2048
    assert 'max_completion_tokens' not in glm_tools
    assert glm_tools.get('thinking') == {'type': 'enabled', 'clear_thinking': False}

    glm_preserved = contract._preserved_thinking(
        ContractSettings(url='http://127.0.0.1:1', model='glm-5.3', protocol='openai', checks=['preserved_thinking']),
        contract.PROBES['preserved_thinking'],
    )
    assert glm_preserved['thinking']['clear_thinking'] is False

    with pytest.raises(contract.Unsupported):
        contract._dynamic_tools(
            ContractSettings(url='http://127.0.0.1:1', model='glm-5.3', protocol='openai', checks=['dynamic_tools']),
            contract.PROBES['dynamic_tools'],
        )


def test_dynamic_tools_probe_matches_postman_system_tools_shape() -> None:
    """KVV: content system + user + tools-only system; tool_choice=required."""
    settings = ContractSettings(url='http://127.0.0.1:1', model='kimi-k3.1-pro', protocol='openai', checks=['dynamic_tools'])
    body = contract._dynamic_tools(settings, contract.PROBES['dynamic_tools'])
    assert body['tool_choice'] == 'required'
    assert body['messages'][0] == {'role': 'system', 'content': '你是助手，请严格使用工具完成查询。'}
    assert body['messages'][1]['role'] == 'user'
    tools_only = body['messages'][2]
    assert tools_only['role'] == 'system'
    assert tools_only.get('content') == ''
    assert tools_only['tools'][0]['function']['name'] == contract.TOOL_NAME


def test_dynamic_tools_gateway_reject_on_kimi_is_a_violation() -> None:
    settings = ContractSettings(url='http://127.0.0.1:1', model='kimi-k3.1-pro', protocol='openai', checks=['dynamic_tools'])
    reply = protocols.Reply(
        status=400,
        error='HTTP 400: The request is invalid.',
        error_type='gateway_error',
        error_code='400001',
    )
    assert contract._judge_dynamic_tools(settings, reply, contract.PROBES['dynamic_tools'])[0] == 'violated'


def test_openai_chat_uses_max_completion_tokens_for_output_budget() -> None:
    settings = ContractSettings(url='https://site.test', model='kimi-k3.1-pro', protocol='openai', checks=['system'])
    body = protocols.build_body(settings, 'hi', stream=False, max_tokens=1024)
    assert body.get('max_completion_tokens') == 1024
    assert 'max_tokens' not in body


def test_empty_content_with_reasoning_is_explained_not_silently_scored() -> None:
    settings = ContractSettings(url='http://127.0.0.1:1', model='kimi-k3.1-pro', protocol='openai', checks=['system'])
    reply = protocols.Reply(reasoning='先想想应该回 SYSTEMOK', status=200)
    verdict, _, detail, _ = contract._judge_system(settings, reply, contract.PROBES['system'])
    assert verdict == 'conformed'
    assert 'SYSTEMOK' in detail or 'system' in detail.lower()

    empty = protocols.Reply(reasoning='无关推理', status=200)
    verdict, _, detail, _ = contract._judge_system(settings, empty, contract.PROBES['system'])
    assert verdict == 'inconclusive'
    assert '思考' in detail


def test_dynamic_tools_mutex_matches_postman_assertions() -> None:
    """Postman 05: 200 = inverted defect; tools+content mutual-exclusion phrase = pass."""
    settings = ContractSettings(
        url='http://127.0.0.1:1', model='kimi-k3.1-pro', protocol='openai', checks=['dynamic_tools_mutex']
    )
    body = contract._dynamic_tools_mutex(settings, contract.PROBES['dynamic_tools_mutex'])
    assert body['messages'][0]['content']
    assert 'content' in body['messages'][2] and 'tools' in body['messages'][2]

    accepted = protocols.Reply(status=200, text='我这边获取不到北京的实时天气')
    assert contract._judge_dynamic_tools_mutex(settings, accepted, contract.PROBES['dynamic_tools_mutex'])[0] == 'violated'

    official = protocols.Reply(
        status=400,
        error='HTTP 400: tools cannot be used with content',
        error_type='invalid_request_error',
    )
    assert contract._judge_dynamic_tools_mutex(settings, official, contract.PROBES['dynamic_tools_mutex'])[0] == 'conformed'

    wrong_phrase = protocols.Reply(
        status=400,
        error='HTTP 400: The request is invalid.',
        error_type='gateway_error',
        error_code='400001',
    )
    assert contract._judge_dynamic_tools_mutex(settings, wrong_phrase, contract.PROBES['dynamic_tools_mutex'])[0] == 'violated'

    generic = protocols.Reply(
        status=400,
        error='HTTP 400: Invalid request',
        error_type='invalid_request_error',
        error_message='Invalid request',
    )
    verdict, _, detail, _ = contract._judge_dynamic_tools_mutex(
        settings, generic, contract.PROBES['dynamic_tools_mutex'],
    )
    assert verdict == 'conformed'
    assert '差异告警' in detail


def test_kvv_dynamic_tools_negative_and_required_bare() -> None:
    """KVV gaps: user-role tools, duplicate names, required without tools → 400."""
    settings = ContractSettings(url='http://127.0.0.1:1', model='kimi-k3.1-pro', protocol='openai')
    role = contract._dynamic_tools_role(settings, contract.PROBES['dynamic_tools_role'])
    assert role['messages'][0]['role'] == 'user' and 'tools' in role['messages'][0]
    dup = contract._dynamic_tools_dup(settings, contract.PROBES['dynamic_tools_dup'])
    assert dup['tools'][0]['function']['name'] == 'dup'
    assert dup['messages'][0]['tools'][0]['function']['name'] == 'dup'
    bare = contract._tool_choice_required_bare(settings, contract.PROBES['tool_choice_required_bare'])
    assert bare['tool_choice'] == 'required'
    assert 'tools' not in bare
    ok = protocols.Reply(status=400, error='HTTP 400: bad request', error_type='invalid_request_error')
    non_400 = protocols.Reply(status=422, error='HTTP 422: unprocessable', error_type='invalid_request_error')
    for check in ('dynamic_tools_role', 'dynamic_tools_dup', 'tool_choice_required_bare'):
        assert contract._judge_expect_http_400(settings, ok, contract.PROBES[check])[0] == 'conformed'
        assert contract._judge_expect_http_400(settings, non_400, contract.PROBES[check])[0] == 'observation'
        assert contract._judge_expect_http_400(
            settings, protocols.Reply(status=200, text='ok'), contract.PROBES[check],
        )[0] == 'violated'


def test_kimi_named_tool_choice_200_is_observation() -> None:
    """KVV: named tool_choice is unsupported; silent 200 is observation, not conformed."""
    settings = ContractSettings(url='https://site.test', model='kimi-k3.1-pro', protocol='openai')
    silent = protocols.Reply(status=200, text='你好')
    assert contract._judge_tool_choice_named(settings, silent, contract.PROBES['tool_choice_named'])[0] == 'observation'
    called = protocols.Reply(
        status=200,
        tool_calls=[{'name': contract.TOOL_NAME, 'arguments': '{"ticket_id":"T-1024"}'}],
    )
    assert contract._judge_tool_choice_named(settings, called, contract.PROBES['tool_choice_named'])[0] == 'observation'


def test_shape_allows_null_tool_calls_and_null_optional_usage() -> None:
    from relay_lab.protocol_validation import shape_errors, usage_errors

    payload = {
        'id': 'x',
        'object': 'chat.completion',
        'created': 1,
        'model': 'm',
        'choices': [{
            'index': 0,
            'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': 'hi', 'tool_calls': None},
        }],
        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': None},
    }
    assert shape_errors('openai', payload, 'application/json') == []
    assert usage_errors('openai', payload) == []


def test_thinking_roundtrip_regex_handles_chinese_prefix() -> None:
    values = set(
        __import__('re').findall(
            r'(?<![A-Za-z0-9_])n\s*(?:[=＝:：]|to|is|为)\s*([1-9]\d{3})(?!\d)',
            '先随机取n=4287，本轮只回复 OK。',
        )
    )
    assert values == {'4287'}


def test_preserved_thinking_matches_postman_injected_n() -> None:
    settings = ContractSettings(
        url='http://127.0.0.1:1', model='kimi-k3.1-pro', protocol='openai', checks=['preserved_thinking']
    )
    probe = contract.PROBES['preserved_thinking']
    body = contract._preserved_thinking(settings, probe)
    assert body['messages'][1]['reasoning_content'].startswith('先随机取 n = 4287')
    assert 'reasoning_effort' not in body
    assert '思考丢失判违规' in probe.expected
    ok = protocols.Reply(text='8574', status=200)
    assert contract._judge_preserved_thinking(settings, ok, probe)[0] == 'conformed'
    # 偏离幅度过大且未提及注入 n=4287，在强制跨轮保留思考的模型下应判 violated
    bad = protocols.Reply(text='12276', status=200)
    assert contract._judge_preserved_thinking(settings, bad, probe)[0] == 'violated'


def test_thinking_probe_follows_selected_protocol_not_forced_anthropic(monkeypatch) -> None:
    """Thinking used to pin Messages wire even when the suite selected Chat."""
    settings = ContractSettings(
        url='https://site.test/v1/chat/completions',
        model='kimi-k3.1-pro',
        protocol='openai',
        checks=['thinking'],
    )
    seen = {}

    def fake_call(client, wire_settings, api_key, body, *, stream, timeout):
        seen['protocol'] = wire_settings.protocol
        seen['url'] = protocols.endpoint(wire_settings)
        seen['body'] = body
        return protocols.Reply(
            text='OK',
            reasoning='4287',
            usage=protocols.Usage(completion_tokens=4, cache_write_tokens=0),
            status=200,
        )

    monkeypatch.setattr(contract, 'call', fake_call)
    item = contract.run_one(object(), settings, None, 'thinking', 1)
    assert item.verdict == 'conformed'
    assert seen['protocol'] == 'openai'
    assert seen['url'] == 'https://site.test/v1/chat/completions'
    assert 'messages' in seen['body']
    assert seen['body']['temperature'] == 1.0
    assert seen['body'].get('reasoning_effort') == 'low'
    assert 'reasoning_content' in item.expected


@pytest.mark.parametrize(
    ('protocol', 'path_suffix', 'body_key', 'expected_token'),
    [
        ('openai', '/v1/chat/completions', 'reasoning_effort', 'reasoning_content'),
        ('openai_responses', '/v1/responses', 'reasoning', 'reasoning'),
        ('anthropic', '/v1/messages', 'thinking', 'thinking 块'),
    ],
)
def test_thinking_probe_builds_per_protocol_enable_knobs(
    protocol, path_suffix, body_key, expected_token,
) -> None:
    settings = ContractSettings(
        url=f'https://site.test{path_suffix}',
        model='kimi-k3.1-pro',
        protocol=protocol,
        checks=['thinking'],
    )
    probe = contract.Probe('thinking', contract._thinking, contract._judge_thinking)
    body = contract._thinking(settings, probe)
    assert body_key in body
    assert expected_token in probe.expected


@pytest.mark.parametrize(
    ('reasoning', 'text', 'expected'),
    [
        ('认真思考', '反证法证明。', 'conformed'),
        ('', '反证法证明。', 'violated'),
        ('', '', 'violated'),
        # 仅有思考没有正文连续 3 次重试后仍无正文 → 判定为 violated
        ('认真思考', '', 'violated'),
    ],
)
def test_thinking_chat_missing_reasoning_content_fails(reasoning, text, expected) -> None:
    settings = ContractSettings(
        url='https://site.test/v1/chat/completions',
        model='kimi-k3.1-pro',
        protocol='openai',
        checks=['thinking'],
    )
    payload = {
        'id': 'chatcmpl-1',
        'object': 'chat.completion',
        'choices': [{
            'index': 0,
            'message': {'role': 'assistant', 'content': text, 'reasoning_content': reasoning or None},
            'finish_reason': 'stop',
        }],
        'usage': {
            'prompt_tokens': 10,
            'completion_tokens': 8,
            'total_tokens': 18,
            'prompt_tokens_details': {'cache_write_tokens': 0},
        },
    }
    if not reasoning:
        payload['choices'][0]['message'].pop('reasoning_content', None)

    def respond(request):
        assert request.url.path == '/v1/chat/completions'
        body = json.loads(request.content)
        assert body.get('reasoning_effort') == 'low'
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        item = contract.run_one(client, settings, None, 'thinking', 1)
    assert item.verdict == expected
    assert item.observed == f'reasoning_content={len(reasoning)} 字符 · text={len(text)} 字符'
    assert contract.summarize([item]).violated == int(expected == 'violated')


@pytest.mark.parametrize('message_type', [None, 'message'])
@pytest.mark.parametrize(
    ('reasoning', 'text', 'expected'),
    [
        ('认真思考', '反证法证明。', 'conformed'),
        ('', '反证法证明。', 'violated'),
        # 仅有思考没有正文连续 3 次重试后仍无正文 → 判定为 violated
        ('认真思考', '', 'violated'),
    ],
)
def test_thinking_anthropic_blocks_still_parse(message_type, reasoning, text, expected) -> None:
    settings = ContractSettings(
        url='https://site.test/v1/messages',
        model='kimi-k3.1-pro',
        protocol='anthropic',
        checks=['thinking'],
    )
    content = []
    if reasoning:
        content.append({'type': 'thinking', 'thinking': reasoning})
    if text:
        content.append({'type': 'text', 'text': text})
    payload = {
        'role': 'assistant',
        'content': content,
        'usage': {'input_tokens': 10, 'output_tokens': 8, 'output_tokens_details': {'thinking_tokens': 4}},
    }
    if message_type is not None:
        payload['type'] = message_type

    def respond(request):
        assert request.url.path == '/v1/messages'
        assert json.loads(request.content)['thinking'] == {'type': 'enabled', 'budget_tokens': 2048}
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        item = contract.run_one(client, settings, None, 'thinking', 1)
    assert item.verdict == expected
    assert item.observed == f'thinking 块={len(reasoning)} 字符 · text={len(text)} 字符'


def test_gateway_error_for_invalid_schema_is_flagged() -> None:
    settings = ContractSettings(url='http://127.0.0.1:1', model='kimi-k3.1-pro', checks=['schema_errors'])
    reply = protocols.Reply(status=400, error='HTTP 400：gateway error', error_type='gateway_error', error_code='400001')
    verdict, _, _, _ = contract._judge_schema_errors(settings, reply, contract.PROBES['schema_errors'])
    assert verdict == 'violated'


def test_schema_errors_requires_postman_error_type_for_all_families() -> None:
    for model in ('kimi-k3.1-pro', 'glm-5.3'):
        settings = ContractSettings(url='http://127.0.0.1:1', model=model, checks=['schema_errors'])
        reply = protocols.Reply(status=400, error='HTTP 400：API 调用参数有误', error_code='400')
        verdict, _, detail, _ = contract._judge_schema_errors(settings, reply, contract.PROBES['schema_errors'])
        assert verdict == 'violated', model
        assert 'error.type' in detail
        bare = protocols.Reply(status=400, error='HTTP 400：非法请求')
        assert contract._judge_schema_errors(settings, bare, contract.PROBES['schema_errors'])[0] == 'violated'


def test_schema_errors_expected_text_follows_the_family_contract() -> None:
    for model in ('kimi-k3.1-pro', 'kimi-k3.1-pro'):
        settings = ContractSettings(url='http://127.0.0.1:1', model=model, checks=['schema_errors'])
        probe = contract.Probe('schema_errors', contract._schema_errors, contract._judge_schema_errors)
        contract._schema_errors(settings, probe)
        assert probe.expected == '400 且 error.type 为 invalid_request_error'


def test_dynamic_system_tools_and_thinking_have_evidence_judges() -> None:
    openai = ContractSettings(url='http://127.0.0.1:1', model='kimi-k3.1-pro', checks=['dynamic_tools'])
    dynamic = protocols.Reply(text='我无法调用工具', status=200)
    assert contract._judge_dynamic_tools(openai, dynamic, contract.PROBES['dynamic_tools'])[0] == 'violated'

    anthropic = ContractSettings(
        url='http://127.0.0.1:1', model='kimi-k3.1-pro', protocol='anthropic', checks=['thinking']
    )
    thinking = protocols.Reply(text='OK', reasoning='四位数是 4287', usage=protocols.Usage(completion_tokens=10))
    assert contract._judge_thinking(anthropic, thinking, contract.PROBES['thinking'])[0] == 'conformed'


def test_missing_cache_write_headers_are_flagged_when_usage_reports_a_write() -> None:
    settings = ContractSettings(url='http://127.0.0.1:1', model='m', checks=['cache_headers'])
    reply = protocols.Reply(status=200, usage=protocols.Usage(cache_write_tokens=512))
    verdict, _, _, _ = contract._judge_cache_headers(settings, reply, contract.PROBES['cache_headers'])
    assert verdict == 'violated'


@pytest.mark.parametrize('protocol', ['openai', 'openai_responses', 'anthropic'])
def test_cache_write_request_uses_fresh_system_prefix_and_explicit_1h_policy(protocol) -> None:
    settings = ContractSettings(url='https://site.test', model='kimi-k3.1-pro', protocol=protocol, checks=['cache_headers'])
    probe = contract.Probe('cache_headers', contract._cache_headers, contract._judge_cache_headers)
    first = contract._cache_headers(settings, probe)
    second = contract._cache_headers(settings, probe)

    def prefix(body):
        if protocol == 'anthropic':
            return body['system']
        if protocol == 'openai_responses':
            return body['instructions']
        return body['messages'][0]['content']

    first_prefix, second_prefix = prefix(first), prefix(second)
    assert first_prefix.startswith('Isolation ')
    assert first_prefix.split('\n', 1)[0] != second_prefix.split('\n', 1)[0]
    assert first_prefix.split('\n', 1)[1] == second_prefix.split('\n', 1)[1]
    assert first_prefix.count('Rule ') >= 48
    assert len(first_prefix) >= 9000
    assert first['stream'] is False
    assert 'stream=false' in probe.expected
    if protocol == 'anthropic':
        assert first['cache_control'] == {'type': 'ephemeral', 'ttl': '1h'}
        assert first['messages'] == [{'role': 'user', 'content': '只回复OK。'}]
        assert first['max_tokens'] == 1024
        assert 'usage.cache_creation.ephemeral_1h_input_tokens' in probe.expected
    else:
        assert first['prompt_cache_options'] == {'mode': 'implicit', 'ttl': '1h'}
        assert 'Msh-Usage-Cache-Write-Tokens-1h' in probe.expected
        if protocol == 'openai':
            assert first['messages'][0]['role'] == 'system'
            assert first['messages'][1] == {'role': 'user', 'content': '只回复OK。'}
            assert first['max_completion_tokens'] == 1024
            assert first['reasoning_effort'] == 'low'
        else:
            assert first['max_output_tokens'] == 1024


@pytest.mark.parametrize('protocol', ['openai', 'openai_responses', 'anthropic'])
def test_cache_write_stream_request_sets_stream_true(protocol) -> None:
    settings = ContractSettings(
        url='https://site.test', model='kimi-k3.1-pro', protocol=protocol, checks=['cache_headers_stream'],
    )
    probe = contract.Probe('cache_headers_stream', contract._cache_headers_stream, contract._judge_cache_headers)
    body = contract._cache_headers_stream(settings, probe)
    assert body['stream'] is True
    assert 'stream=true' in probe.expected
    if protocol == 'openai':
        assert body['stream_options'] == {'include_usage': True}


@pytest.mark.parametrize('protocol', ['openai', 'openai_responses'])
def test_cache_write_5m_request_uses_5m_ttl_and_header(protocol) -> None:
    settings = ContractSettings(
        url='https://site.test', model='kimi-k3.1-pro', protocol=protocol, checks=['cache_headers_5m'],
    )
    probe = contract.Probe('cache_headers_5m', contract._cache_headers_5m, contract._judge_cache_headers)
    body = contract._cache_headers_5m(settings, probe)
    assert body['prompt_cache_options'] == {'mode': 'implicit', 'ttl': '5m'}
    assert 'Msh-Usage-Cache-Write-Tokens-5m' in probe.expected
    usage = {
        'cache_creation_input_tokens': 512,
        'cache_read_input_tokens': 0,
        'prompt_tokens_details' if protocol == 'openai' else 'input_tokens_details': {'cache_write_tokens': 512},
    }
    payload = {'choices': [{'message': {'content': 'OK'}}], 'usage': usage}
    if protocol == 'openai_responses':
        payload = {
            'object': 'response',
            'output_text': 'OK',
            'store': False,
            'background': False,
            'previous_response_id': None,
            'conversation': None,
            'usage': usage,
        }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=payload,
            headers={'Msh-Usage-Cache-Write-Tokens-5m': '512', 'Msh-Usage-Cache-Write-Tokens-1h': '0'},
        ),
    )
    with httpx.Client(transport=transport) as client:
        item = contract.run_one(client, settings, None, 'cache_headers_5m', 1)
    assert item.verdict == 'conformed', item.detail


def test_fixed_sampling_and_thinking_disable_reject_or_keep_thinking() -> None:
    settings = ContractSettings(url='https://site.test', model='kimi-k3.1-pro', protocol='openai')
    probe = contract.Probe('fixed_sampling', contract._fixed_sampling, contract._judge_fixed_sampling)
    body = contract._fixed_sampling(settings, probe)
    assert body['temperature'] == 0.6
    reply = protocols.Reply(status=400, error='temperature must be 1.0')
    assert contract._judge_fixed_sampling(settings, reply, probe)[0] == 'conformed'
    probe = contract.Probe('thinking_disable', contract._thinking_disable, contract._judge_thinking_disable)
    body = contract._thinking_disable(settings, probe)
    assert body['thinking'] == {'type': 'disabled'}
    assert 'reasoning_effort' not in body
    # K3 locked thinking: keeping reasoning or clean 400 is conformed; clearing is violated.
    kept = protocols.Reply(status=200, text='391', reasoning='calc')
    assert contract._judge_thinking_disable(settings, kept, probe)[0] == 'conformed'
    cleared = protocols.Reply(status=200, text='391')
    assert contract._judge_thinking_disable(settings, cleared, probe)[0] == 'violated'
    # K2.5 may disable thinking: 200 without reasoning is conformed.
    k25 = ContractSettings(url='https://site.test', model='kimi-k2.5', protocol='openai')
    assert contract._judge_thinking_disable(k25, cleared, probe)[0] == 'conformed'
    assert contract._judge_thinking_disable(k25, kept, probe)[0] == 'violated'


def test_partial_mode_and_vision_svg_reject_judges() -> None:
    settings = ContractSettings(url='https://site.test', model='kimi-k3.1-pro', protocol='openai')
    probe = contract.Probe('partial_mode', contract._partial_mode, contract._judge_partial_mode)
    body = contract._partial_mode(settings, probe)
    assert body['messages'][-1] == {
        'role': 'assistant',
        'content': contract._PARTIAL_MODE_PREFIX,
        'partial': True,
    }
    assert body['messages'][-2]['content'] == '1+1等于几？'
    assert contract._judge_partial_mode(
        settings, protocols.Reply(status=200, text=contract._PARTIAL_MODE_SUFFIX), probe,
    )[0] == 'conformed'
    guessed = protocols.Reply(status=200, text=contract._PARTIAL_MODE_SUFFIX, reasoning='1+1=2')
    assert contract._judge_partial_mode(settings, guessed, probe)[0] == 'violated'
    probe = contract.Probe('vision_svg_reject', contract._vision_svg_reject, contract._judge_vision_svg_reject)
    body = contract._vision_svg_reject(settings, probe)
    assert body['messages'][-1]['content'][1]['image_url']['url'].startswith('data:image/svg+xml')
    assert contract._judge_vision_svg_reject(settings, protocols.Reply(status=400, error='unsupported'), probe)[0] == 'conformed'
    # A5: upstream jitter is inconclusive; a bare 5xx remap without upstream markers stays violated.
    assert contract._judge_vision_svg_reject(
        settings, protocols.Reply(status=502, error='upstream', error_type='upstream_error'), probe,
    )[0] == 'inconclusive'
    assert contract._judge_vision_svg_reject(settings, protocols.Reply(status=502, error='svg remap'), probe)[0] == 'violated'


def test_anthropic_thinking_carry_effort_and_roundtrip_are_expressible() -> None:
    """These three used to skip on Anthropic with empty rows; they must build native bodies."""
    settings = ContractSettings(url='https://site.test', model='kimi-k3.1-pro', protocol='anthropic')
    preserved = contract._preserved_thinking(settings, contract.PROBES['preserved_thinking'])
    assert preserved['thinking'] == {'type': 'enabled', 'budget_tokens': 2048}
    assert preserved['output_config'] == {'effort': 'low'}
    assert preserved['messages'][1]['content'][0]['type'] == 'thinking'
    assert f'n = {contract.PRESERVED_N}' in preserved['messages'][1]['content'][0]['thinking']
    assert contract._judge_preserved_thinking(
        settings, protocols.Reply(status=200, text=contract.PRESERVED_DOUBLE), contract.PROBES['preserved_thinking'],
    )[0] == 'conformed'
    effort = contract._reasoning_effort(settings, contract.PROBES['reasoning_effort'])
    assert effort['thinking']['type'] == 'enabled'
    assert effort['output_config']['effort'] == 'low'
    assert contract._judge_reasoning_effort(
        settings, protocols.Reply(status=200, text='399', reasoning='calc'), contract.PROBES['reasoning_effort'],
    )[0] == 'conformed'
    trip = contract._thinking_roundtrip(settings, contract.PROBES['thinking_roundtrip'])
    assert trip['thinking']['budget_tokens'] == 2048
    assert 'output_config' not in trip
    openai_trip = contract._thinking_roundtrip(
        ContractSettings(url='https://site.test', model='kimi-k3.1-pro', protocol='openai'),
        contract.PROBES['thinking_roundtrip'],
    )
    assert 'reasoning_effort' not in openai_trip
    assert contract._extract_roundtrip_n(openai_trip['messages'][-1]['content']) == set()


def test_gap_probes_from_retest_kimi_cover_tool_choice_cache_and_vision_edges() -> None:
    settings = ContractSettings(url='https://site.test', model='kimi-k3.1-pro', protocol='openai')
    none_body = contract._tool_choice_none(settings, contract.PROBES['tool_choice_none'])
    assert none_body['tool_choice'] == 'none'
    assert contract._judge_tool_choice_none(
        settings, protocols.Reply(status=200, text='工单处理中'), contract.PROBES['tool_choice_none'],
    )[0] == 'conformed'
    assert contract._judge_tool_choice_none(
        settings,
        protocols.Reply(status=200, text='', tool_calls=[{'name': 'lookup_ticket', 'arguments': '{}'}]),
        contract.PROBES['tool_choice_none'],
    )[0] == 'violated'
    required = contract._tool_choice_required(settings, contract.PROBES['tool_choice_required'])
    assert required['tool_choice'] == 'required'
    assert contract._judge_tool_choice_required(
        settings,
        protocols.Reply(status=200, tool_calls=[{'name': 'lookup_ticket', 'arguments': '{}'}]),
        contract.PROBES['tool_choice_required'],
    )[0] == 'conformed'
    bp = contract._cache_breakpoint(settings, contract.PROBES['cache_breakpoint'])
    assert any(
        isinstance(part, dict) and part.get('type') == 'prompt_cache_breakpoint'
        for part in bp['messages'][0]['content']
    )
    assert contract._judge_cache_breakpoint(
        settings, protocols.Reply(status=400, error='breakpoint'), contract.PROBES['cache_breakpoint'],
    )[0] == 'conformed'
    assert contract._judge_cache_breakpoint(
        settings, protocols.Reply(status=200, text='ok'), contract.PROBES['cache_breakpoint'],
    )[0] == 'violated'
    vision = contract._vision_base64(settings.model_copy(update={'protocol': 'anthropic'}), contract.PROBES['vision_base64'])
    assert vision['messages'][-1]['content'][0]['type'] == 'image'
    log_body = contract._logprobs(settings, contract.PROBES['logprobs'])
    assert log_body['logprobs'] is True and log_body['top_logprobs'] == 1
    mix = contract._partial_json_mix(settings, contract.PROBES['partial_json_mix'])
    assert mix['response_format'] == {'type': 'json_object'}
    assert mix['messages'][-1]['partial'] is True
    assert contract.request_cost('tool_followup') == 3


def test_official_client_reject_on_optional_knobs_is_conformed() -> None:
    """Expected「规范 4xx」must score as pass when the gateway returns a clean refusal."""
    settings = ContractSettings(url='https://site.test', model='kimi-k3.1-pro', protocol='openai')
    # 官方原文：指定函数与思考不兼容，思考开启时传 400。K3 恒思考，400 即官方行为。
    named = protocols.Reply(
        status=400,
        error="Invalid parameter: 'tool_choice' does not support specifying a function",
        error_type='invalid_request_error',
    )
    assert contract._judge_tool_choice_named(settings, named, contract.PROBES['tool_choice_named'])[0] == 'conformed'

    vision_url = protocols.Reply(
        status=400,
        error='Invalid parameter: unsupported image url (public image URLs are not supported)',
        error_type='invalid_request_error',
    )
    assert contract._judge_vision_accept(settings, vision_url, contract.PROBES['vision_url'])[0] == 'conformed'

    vision_video = protocols.Reply(
        status=400,
        error='video media is invalid or damaged',
        error_type='new_api_error',
        error_code='video_unsupported_format',
    )
    assert contract._judge_vision_accept(settings, vision_video, contract.PROBES['vision_video'])[0] == 'conformed'

    logprobs = protocols.Reply(
        status=400,
        error="Invalid parameter: 'logprobs' and 'top_logprobs' are not supported",
        error_type='invalid_request_error',
    )
    # Kimi / OpenAI 兼容官方支持 logprobs，400 拒绝或 200 缺失字段均判 violated；GLM 保持 observation。
    assert contract._judge_logprobs(settings, logprobs, contract.PROBES['logprobs'])[0] == 'violated'
    dropped_logprobs = protocols.Reply(status=200, payload={'choices': [{'message': {'content': 'OK'}}]})
    assert contract._judge_logprobs(settings, dropped_logprobs, contract.PROBES['logprobs'])[0] == 'violated'
    glm_settings = ContractSettings(url='https://site.test', model='glm-5.3', protocol='openai')
    assert contract._judge_logprobs(glm_settings, logprobs, contract.PROBES['logprobs'])[0] == 'observation'
    assert contract._judge_logprobs(glm_settings, dropped_logprobs, contract.PROBES['logprobs'])[0] == 'observation'

    # Named tool_choice 403 with a substantive body is still observation (A6), not conformed.
    forbid_param = protocols.Reply(
        status=403,
        error='HTTP 403：tool_choice function name is not supported for this model',
        error_type='invalid_request_error',
    )
    assert contract._judge_tool_choice_named(settings, forbid_param, contract.PROBES['tool_choice_named'])[0] == 'observation'

    mix = protocols.Reply(
        status=400,
        error='HTTP 400：partial and json_object cannot be combined',
        error_type='invalid_request_error',
    )
    assert contract._judge_partial_json_mix(settings, mix, contract.PROBES['partial_json_mix'])[0] == 'conformed'

    for judge, probe in (
        (contract._judge_fixed_sampling, contract.PROBES['fixed_sampling']),
        (contract._judge_cache_breakpoint, contract.PROBES['cache_breakpoint']),
    ):
        ok = protocols.Reply(status=400, error='HTTP 400：rejected', error_type='invalid_request_error')
        assert judge(settings, ok, probe)[0] == 'conformed'
        rate = protocols.Reply(status=429, error='HTTP 429：rate limit', error_type='rate_limit_error')
        assert judge(settings, rate, probe)[0] == 'inconclusive'

    # K3 locked thinking: rejecting disabled is a pass.
    disabled_reject = protocols.Reply(
        status=400, error='HTTP 400：rejected', error_type='invalid_request_error',
    )
    assert contract._judge_thinking_disable(
        settings, disabled_reject, contract.PROBES['thinking_disable'],
    )[0] == 'conformed'
    # Unofficial disable knobs still treat a clean 400 as conformed.
    assert contract._judge_thinking_disable(
        settings, disabled_reject, contract.PROBES['thinking_disable_enable'],
    )[0] == 'conformed'
    # K2.5 allows disable: rejecting it is a violation.
    k25 = ContractSettings(url='https://site.test', model='kimi-k2.5', protocol='openai')
    assert contract._judge_thinking_disable(
        k25, disabled_reject, contract.PROBES['thinking_disable'],
    )[0] == 'violated'

    # Auth/rate-limit 4xx still cannot prove the parameter contract.
    auth = protocols.Reply(status=401, error='invalid_api_key', error_type='invalid_request_error')
    assert contract._judge_logprobs(settings, auth, contract.PROBES['logprobs'])[0] == 'inconclusive'
    assert contract._judge_vision_accept(settings, auth, contract.PROBES['vision_url'])[0] == 'inconclusive'
    permission = protocols.Reply(status=403, error='HTTP 403：forbidden', error_type='invalid_request_error')
    assert contract._judge_logprobs(settings, permission, contract.PROBES['logprobs'])[0] == 'inconclusive'

    # Synthesized empty status line is not enough evidence of an official refusal.
    empty = protocols.Reply(status=400, error='HTTP 400：')
    assert contract._judge_logprobs(settings, empty, contract.PROBES['logprobs'])[0] == 'inconclusive'
    assert contract._official_client_reject(empty) is False


@pytest.fixture
def cache_write_result():
    """Exercise request execution, wire-field parsing, verdicts and report aggregation."""
    def run(protocol, usage, *, headers=None, status=200):
        settings = ContractSettings(url='https://site.test', model='kimi-k3.1-pro', protocol=protocol, checks=['cache_headers'])
        usage_copy = dict(usage)
        if protocol == 'anthropic':
            usage_copy.setdefault('output_tokens_details', {'thinking_tokens': 0})
            payload = {'type': 'message', 'content': [{'type': 'text', 'text': 'OK'}], 'usage': usage_copy}
        elif protocol == 'openai_responses':
            if 'input_tokens_details' not in usage_copy and isinstance(usage_copy.get('cache_creation_input_tokens'), int) and not isinstance(usage_copy.get('cache_creation_input_tokens'), bool):
                usage_copy['input_tokens_details'] = {'cache_write_tokens': usage_copy['cache_creation_input_tokens']}
            payload = {
                'object': 'response',
                'output_text': 'OK',
                'store': False,
                'background': False,
                'previous_response_id': None,
                'conversation': None,
                'usage': usage_copy,
            }
        else:
            if 'prompt_tokens_details' not in usage_copy and isinstance(usage_copy.get('cache_creation_input_tokens'), int) and not isinstance(usage_copy.get('cache_creation_input_tokens'), bool):
                usage_copy['prompt_tokens_details'] = {'cache_write_tokens': usage_copy['cache_creation_input_tokens']}
            payload = {'choices': [{'message': {'content': 'OK'}}], 'usage': usage_copy}
        if status != 200:
            payload = {'error': {'type': 'invalid_request_error', 'message': 'cache policy rejected'}}
        resp_headers = dict(headers or {})
        if protocol in {'openai', 'openai_responses'} and 'Msh-Usage-Cache-Write-Tokens-1h' in resp_headers and 'Msh-Usage-Cache-Write-Tokens-5m' not in resp_headers:
            resp_headers['Msh-Usage-Cache-Write-Tokens-5m'] = '0'
        transport = httpx.MockTransport(lambda request: httpx.Response(status, json=payload, headers=resp_headers))
        with httpx.Client(transport=transport) as client:
            item = contract.run_one(client, settings, None, 'cache_headers', 1)
        summary = contract.summarize([item])
        assert summary.conformed == int(item.verdict == 'conformed')
        assert summary.violated == int(item.verdict == 'violated')
        assert summary.observation == int(item.verdict == 'observation')
        assert summary.inconclusive == int(item.verdict == 'inconclusive')
        return item

    return run


@pytest.mark.parametrize(
    ('protocol', 'usage'),
    [
        ('openai', {'prompt_tokens_details': {'cache_write_tokens': 512, 'cached_tokens': 0}}),
        ('openai', {'cache_creation_input_tokens': 512, 'cache_read_input_tokens': 0}),
        ('openai_responses', {'input_tokens_details': {'cache_write_tokens': 512, 'cached_tokens': 0}}),
        ('openai_responses', {'cache_creation_input_tokens': 512, 'cache_read_input_tokens': 0}),
        ('anthropic', {'cache_creation_input_tokens': 512, 'cache_read_input_tokens': 0,
                       'cache_creation': {'ephemeral_1h_input_tokens': 512}}),
        ('anthropic', {'prompt_tokens_details': {'cache_write_tokens': 512, 'cached_tokens': 0},
                       'cache_creation': {'ephemeral_1h_input_tokens': 512}}),
    ],
)
def test_cache_write_accepts_postman_usage_shapes(cache_write_result, protocol, usage) -> None:
    item = cache_write_result(protocol, usage, headers={'Msh-Usage-Cache-Write-Tokens-1h': '512'})
    assert item.verdict == 'conformed'
    assert 'write=512' in item.observed
    assert 'read=0' in item.observed
    assert '未验证实际 1h 存活' in item.detail


@pytest.mark.parametrize('protocol', ['openai', 'openai_responses'])
@pytest.mark.parametrize('header', [None, '', 'invalid', 'NaN', 'Infinity'])
def test_openai_cache_write_requires_positive_numeric_1h_header(cache_write_result, protocol, header) -> None:
    headers = {'Msh-Usage-Cache-Write-Tokens-5m': '512'}
    if header is not None:
        headers['Msh-Usage-Cache-Write-Tokens-1h'] = header
    usage = {'cache_creation_input_tokens': 512, 'cache_read_input_tokens': 0}
    item = cache_write_result(protocol, usage, headers=headers)
    assert item.verdict == 'violated'
    assert 'Msh-Usage-Cache-Write-Tokens-1h' in item.detail


@pytest.mark.parametrize('protocol', ['openai', 'openai_responses'])
@pytest.mark.parametrize('header', ['0', '-1'])
def test_openai_cache_write_zero_header_is_violated(cache_write_result, protocol, header) -> None:
    """对齐官方标准：隔离长前缀写入检查中，目标 TTL 响应头为 0 或负数直接判违规。"""
    usage = {'cache_creation_input_tokens': 512, 'cache_read_input_tokens': 0}
    item = cache_write_result(
        protocol, usage, headers={'Msh-Usage-Cache-Write-Tokens-1h': header, 'Msh-Usage-Cache-Write-Tokens-5m': '512'},
    )
    assert item.verdict == 'violated'
    assert f'Msh-Usage-Cache-Write-Tokens-1h={header!r}' in item.detail


@pytest.mark.parametrize('counter', [None, 0, -1, 'invalid', 'NaN', 'Infinity', True])
def test_messages_cache_write_zero_or_missing_1h_usage_is_violated(cache_write_result, counter) -> None:
    """对齐官方标准：Anthropic 必须通过 usage.cache_creation.ephemeral_* 报告正数写入，响应头不能替代。"""
    usage = {'cache_creation_input_tokens': 512, 'cache_read_input_tokens': 0,
             'cache_creation': {'ephemeral_5m_input_tokens': 512}}
    if counter is not None:
        usage['cache_creation']['ephemeral_1h_input_tokens'] = counter
    item = cache_write_result('anthropic', usage, headers={'Msh-Usage-Cache-Write-Tokens-1h': '512'})
    assert item.verdict == 'violated'
    assert 'usage.cache_creation.ephemeral_1h_input_tokens' in item.detail


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('counter', [None, 'invalid', 'NaN', 'Infinity', True])
def test_cache_write_positive_ttl_cannot_hide_missing_or_invalid_write(cache_write_result, protocol, counter) -> None:
    usage = {'cache_read_input_tokens': 0, 'cache_creation': {'ephemeral_1h_input_tokens': 512}}
    if counter is not None:
        usage['cache_creation_input_tokens'] = counter
    item = cache_write_result(protocol, usage, headers={'Msh-Usage-Cache-Write-Tokens-1h': '512'})
    assert item.verdict == 'violated'
    assert '缓存写入计数' in item.detail


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('counter', [0, -1])
def test_cache_write_zero_usage_is_violated(cache_write_result, protocol, counter) -> None:
    """对齐官方标准：隔离长前缀冷发时，usage 写入计数为 0 或负数直接判违规。"""
    usage = {
        'cache_creation_input_tokens': counter,
        'cache_read_input_tokens': 0,
        'cache_creation': {'ephemeral_1h_input_tokens': 512},
    }
    item = cache_write_result(protocol, usage, headers={'Msh-Usage-Cache-Write-Tokens-1h': '512'})
    assert item.verdict == 'violated'
    assert '缓存写入计数' in item.detail


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('counter', [1, -1, 'invalid', 'NaN', 'Infinity', False])
def test_cache_write_fresh_prefix_must_not_report_a_hit(cache_write_result, protocol, counter) -> None:
    usage = {'cache_creation_input_tokens': 512, 'cache_read_input_tokens': counter,
             'cache_creation': {'ephemeral_1h_input_tokens': 512}}
    item = cache_write_result(protocol, usage, headers={'Msh-Usage-Cache-Write-Tokens-1h': '512'})
    assert item.verdict == 'violated'
    assert '缓存读取计数应为 0' in item.detail


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_cache_write_omitted_read_counter_uses_postman_zero_fallback(cache_write_result, protocol) -> None:
    usage = {'cache_creation_input_tokens': 512, 'cache_creation': {'ephemeral_1h_input_tokens': 512}}
    item = cache_write_result(protocol, usage, headers={'Msh-Usage-Cache-Write-Tokens-1h': '512'})
    assert item.verdict == 'conformed'
    assert '缺失（按 0）' in item.observed


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('status', [400, 403])
def test_cache_write_http_failure_is_a_failed_assertion(cache_write_result, protocol, status) -> None:
    item = cache_write_result(protocol, {}, status=status)
    assert item.verdict == 'violated'
    assert f'HTTP {status}，期望 200' in item.detail


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_cache_write_upstream_5xx_is_inconclusive(cache_write_result, protocol) -> None:
    """A5: upstream jitter must not count as a cache-header rewrite."""
    def run_upstream(request):
        return httpx.Response(
            502,
            headers={'content-type': 'application/json'},
            json={'error': {'message': 'Bad Gateway', 'type': 'upstream_error'}},
        )

    settings = ContractSettings(
        url='https://site.test', model='kimi-k3.1-pro', protocol=protocol, checks=['cache_headers'],
    )
    with httpx.Client(transport=httpx.MockTransport(run_upstream)) as client:
        item = contract.run_one(client, settings, 'sk', 'cache_headers', 1)
    assert item.verdict == 'inconclusive'
    assert '上游故障' in item.detail


def test_cache_write_usage_zero_primary_counter_does_not_fall_back_to_positive_details(cache_write_result) -> None:
    """主写入字段为 0 时不得回退使用 details=512 洗白，按官方标准判违规。"""
    usage = {'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
             'prompt_tokens_details': {'cache_write_tokens': 512, 'cached_tokens': 0}}
    item = cache_write_result('openai', usage, headers={'Msh-Usage-Cache-Write-Tokens-1h': '512'})
    assert item.verdict == 'violated'
    assert 'write=0' in item.observed


def test_cache_write_null_primary_counter_falls_back_to_details(cache_write_result) -> None:
    usage = {'cache_creation_input_tokens': None, 'cache_read_input_tokens': None,
             'prompt_tokens_details': {'cache_write_tokens': 512, 'cached_tokens': 0}}
    item = cache_write_result('openai', usage, headers={'Msh-Usage-Cache-Write-Tokens-1h': '512'})
    assert item.verdict == 'conformed'


def test_cache_write_transport_failure_and_unsupported_protocol_remain_inconclusive() -> None:
    def offline(request):
        raise httpx.ConnectError('offline')

    with httpx.Client(transport=httpx.MockTransport(offline)) as client:
        settings = ContractSettings(url='https://site.test', model='m', checks=['cache_headers'])
        item = contract.run_one(client, settings, None, 'cache_headers', 1)
        assert item.verdict == 'inconclusive'
        settings = settings.model_copy(update={'protocol': 'gemini'})
        item = contract.run_one(client, settings, None, 'cache_headers', 1)
        assert item.verdict == 'inconclusive'
        assert '协议无法表达' in item.detail


def test_mixed_upstream_versions_are_reported_as_routing_instability() -> None:
    settings = ContractSettings(url='http://127.0.0.1:1', model='m', checks=['routing'])
    probe = contract.PROBES['routing']
    probe.routing_versions = ['v1.0.0-rc.44', 'v1.0.0-rc.47', 'v1.0.0-rc.44']
    verdict, _, _, observed = contract._judge_routing(settings, protocols.Reply(), probe)
    assert verdict == 'violated'
    assert 'rc.47' in observed


# ---------- and must not accuse an honest relay ----------


def test_honest_relay_is_never_accused(honest) -> None:
    items = contract.run_suite(settings_for(honest, CHECKS), None)
    wrongly = [f'{item.protocol}/{item.check}' for item in items if item.verdict == 'violated']
    assert wrongly == [], f'false accusation on {wrongly}'
    for protocol in ('openai', 'openai_responses', 'anthropic'):
        got = verdicts(items, protocol)
        assert got['stream'] == 'conformed'
        assert got['system'] == 'conformed'


def test_honest_relay_cache_without_counters_is_inconclusive(honest) -> None:
    """Silence about caching must never be read as a cache violation."""
    summary = contract.summarize(contract.run_suite(settings_for(honest, ['cache']), None))
    assert summary.violated == 0
    assert summary.inconclusive == 3


# ---------- verdict arithmetic ----------


def test_fidelity_ignores_inconclusive_rows_rather_than_diluting_them() -> None:
    items = [
        ContractItem(index=1, check='tools', title='t', verdict='conformed', severity='high'),
        ContractItem(index=2, check='stop', title='t', verdict='violated', severity='high'),
        ContractItem(index=3, check='cache', title='t', verdict='inconclusive', severity='low'),
    ]
    summary = contract.summarize(items)
    assert summary.fidelity == 50.0
    assert summary.inconclusive == 1
    assert summary.high_severity == 1


def test_high_severity_counts_only_violations_not_inconclusive_rows() -> None:
    """An inconclusive high-severity check is a gap in evidence, not a finding."""
    items = [ContractItem(index=1, check='tools', title='t', verdict='inconclusive', severity='high')]
    assert contract.summarize(items).high_severity == 0


def test_summary_of_nothing_has_no_fidelity_claim() -> None:
    summary = contract.summarize([])
    assert summary.total == 0
    assert summary.fidelity == 0


def test_unknown_check_name_is_rejected_instead_of_silently_skipped() -> None:
    with pytest.raises(ValidationError):
        ContractSettings(url='http://127.0.0.1:1', model='m', checks=['tools', 'telepathy'])


def test_authorization_is_required_for_contract_runs() -> None:
    with pytest.raises(ValidationError):
        ContractRequest(url='http://127.0.0.1:1', model='m', authorized=False)


def test_stopping_mid_suite_keeps_completed_rows(honest) -> None:
    seen: list[ContractItem] = []
    items = contract.run_suite(
        settings_for(honest, CHECKS), None, should_stop=lambda: len(seen) >= 2, on_item=seen.append
    )
    assert len(items) == 2
    assert all(item.verdict != 'violated' for item in items)


# ---------- protocol adapters ----------


@pytest.mark.parametrize(
    ('protocol', 'url', 'expected'),
    [
        ('openai', 'https://site.test', 'https://site.test/v1/chat/completions'),
        ('openai', 'https://site.test/v1', 'https://site.test/v1/chat/completions'),
        ('openai', 'https://site.test/proxy/v1', 'https://site.test/proxy/v1/chat/completions'),
        ('openai_responses', 'https://site.test/v1', 'https://site.test/v1/responses'),
        ('anthropic', 'https://site.test', 'https://site.test/v1/messages'),
        ('anthropic', 'https://site.test/v1/messages', 'https://site.test/v1/messages'),
        ('gemini', 'https://site.test', 'https://site.test/v1beta/models/m:generateContent'),
        ('gemini', 'https://site.test/proxy/v1beta', 'https://site.test/proxy/v1beta/models/m:generateContent'),
    ],
)
def test_endpoints_never_duplicate_a_version_segment(protocol: str, url: str, expected: str) -> None:
    """A doubled /v1 404s or, worse, hits a SPA catch-all and returns HTML with a 200."""
    assert protocols.endpoint(ContractSettings(url=url, model='m', protocol=protocol, checks=['system'])) == expected


def test_anthropic_uses_its_own_auth_and_never_leaks_the_key_into_the_url() -> None:
    settings = ContractSettings(url='https://site.test', model='m', protocol='anthropic', checks=['system'])
    headers = protocols.headers_for(settings, 'sk-secret')
    assert headers['x-api-key'] == 'sk-secret'
    assert 'Authorization' not in headers
    assert 'anthropic-version' in headers
    assert 'sk-secret' not in protocols.endpoint(settings)


@pytest.mark.parametrize(
    'base',
    [
        'https://api.moonshot.cn',
        'https://api.moonshot.cn/v1',
        'https://api.moonshot.cn/anthropic',
        'https://api.moonshot.cn/anthropic/v1',
        'https://api.moonshot.cn/v1/chat/completions',
        'https://api.moonshot.cn/v1/responses',
        'https://api.moonshot.cn/anthropic/v1/messages',
    ],
)
def test_moonshot_three_wires_resolve_from_any_base(base: str) -> None:
    """A1/A10: one Moonshot site root must drive Chat / Responses / Anthropic without path wash."""
    # Strip first — ContractSettings rejects a Chat URL paired with Responses/Anthropic.
    root = contract._api_root(base)
    assert root == 'https://api.moonshot.cn'
    chat = ContractSettings(url=root, model='kimi-k2', protocol='openai', checks=['system'])
    responses = ContractSettings(url=root, model='kimi-k2', protocol='openai_responses', checks=['system'])
    anthropic = ContractSettings(url=root, model='kimi-k2', protocol='anthropic', checks=['system'])
    assert protocols.endpoint(chat) == 'https://api.moonshot.cn/v1/chat/completions'
    assert protocols.endpoint(responses) == 'https://api.moonshot.cn/v1/responses'
    assert protocols.endpoint(anthropic) == 'https://api.moonshot.cn/anthropic/v1/messages'


def test_moonshot_anthropic_uses_bearer_auth() -> None:
    """A2/A10: Moonshot Anthropic mount authenticates with Bearer, not x-api-key."""
    settings = ContractSettings(
        url='https://api.moonshot.cn', model='kimi-k2', protocol='anthropic', checks=['system'],
    )
    headers = protocols.headers_for(settings, 'sk-moonshot')
    assert headers['Authorization'] == 'Bearer sk-moonshot'
    assert 'x-api-key' not in headers
    assert headers['anthropic-version']
    assert 'sk-moonshot' not in protocols.endpoint(settings)


def test_glm_cache_headers_are_unsupported_not_violated() -> None:
    """A3: non-Kimi families must raise Unsupported instead of failing on missing Msh headers."""
    settings = ContractSettings(url='https://site.test', model='glm-5.3', protocol='openai', checks=['cache_headers'])
    with pytest.raises(protocols.Unsupported):
        contract._cache_headers(settings, contract.PROBES['cache_headers'])


def test_cache_options_echo_is_responses_only_and_scores_echo() -> None:
    """A8 / P0-2: Responses echo needs a cacheable prefix; no-write missing echo is soft."""
    chat = ContractSettings(url='https://site.test', model='kimi-k2', protocol='openai', checks=['cache_options_echo'])
    with pytest.raises(protocols.Unsupported):
        contract._cache_options_echo(chat, contract.PROBES['cache_options_echo'])
    assert contract.PROBES['cache_options_echo'].wire_protocol == 'openai_responses'
    settings = ContractSettings(
        url='https://site.test', model='kimi-k2', protocol='openai_responses', checks=['cache_options_echo'],
    )
    body = contract._cache_options_echo(settings, contract.PROBES['cache_options_echo'])
    assert body['prompt_cache_options'] == {'mode': 'implicit', 'ttl': '5m'}
    assert 'instructions' in body and len(body['instructions']) > 500
    ok = protocols.Reply(
        status=200,
        payload={'prompt_cache_options': {'mode': 'implicit', 'ttl': '5m'}, 'output_text': 'OK'},
    )
    assert contract._judge_cache_options_echo(settings, ok, contract.PROBES['cache_options_echo'])[0] == 'conformed'
    # 未回显 prompt_cache_options 但已产生缓存写入且两档写档头完整 → 仅记观察（P2 低危差异），不计入主违规
    write_ok_headers_no_echo = protocols.Reply(
        status=200,
        payload={'output_text': 'OK'},
        headers={'msh-usage-cache-write-tokens-5m': '512', 'msh-usage-cache-write-tokens-1h': '0'},
        usage=protocols.Usage(cache_write_tokens=512, cached_tokens=0),
    )
    verdict_obs, sev_obs, _, _ = contract._judge_cache_options_echo(
        settings, write_ok_headers_no_echo, contract.PROBES['cache_options_echo']
    )
    assert verdict_obs == 'observation' and sev_obs == 'low'
    # 未回显且写档头缺失 → 仍判 violated
    write_missing_headers = protocols.Reply(
        status=200,
        payload={'output_text': 'OK'},
        headers={},
        usage=protocols.Usage(cache_write_tokens=512, cached_tokens=0),
    )
    assert contract._judge_cache_options_echo(settings, write_missing_headers, contract.PROBES['cache_options_echo'])[0] == 'violated'
    bad_echo = protocols.Reply(
        status=200,
        payload={'prompt_cache_options': {'mode': 'explicit', 'ttl': '10m'}, 'output_text': 'OK'},
    )
    assert contract._judge_cache_options_echo(settings, bad_echo, contract.PROBES['cache_options_echo'])[0] == 'violated'


def test_suite_skips_cache_options_echo_on_chat_wire() -> None:
    """Chat column must not show「无法表达」for a Responses-only probe."""
    settings = ContractSettings(
        url='https://site.test',
        model='kimi-k3.1-pro',
        protocol='openai',
        protocols=['openai', 'openai_responses'],
        checks=['cache_options_echo'],
    )

    def respond(request):
        path = str(request.url.path)
        if path.endswith('/responses'):
            return httpx.Response(
                200,
                json={
                    'id': 'r', 'object': 'response', 'status': 'completed', 'model': 'm',
                    'store': False, 'background': False, 'previous_response_id': None, 'conversation': None,
                    'output': [{'type': 'message', 'role': 'assistant',
                                'content': [{'type': 'output_text', 'text': 'OK'}]}],
                    'prompt_cache_options': {'mode': 'implicit', 'ttl': '5m'},
                    'usage': {
                        'input_tokens': 10,
                        'output_tokens': 1,
                        'total_tokens': 11,
                        'input_tokens_details': {'cache_write_tokens': 0},
                    },
                },
            )
        raise AssertionError(f'Chat wire must not invoke cache_options_echo: {path}')

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        items = []
        for protocol in contract.select_protocols(settings):
            wire = contract._settings_for_protocol(settings, protocol)
            for check in contract.select(settings):
                required = contract.PROBES[check].wire_protocol
                if required and required != protocol:
                    continue
                items.append(contract.run_one(client, wire, None, check, len(items) + 1))
    assert len(items) == 1
    assert items[0].protocol == 'openai_responses'
    assert items[0].check == 'cache_options_echo'
    assert items[0].verdict == 'conformed'


def test_suite_omits_unsupported_protocol_probes(monkeypatch) -> None:
    """不支持的协议不应生成“协议无法表达该检查”的冗余检查项。"""
    settings = ContractSettings(
        url='https://site.test',
        model='kimi-k3.1-pro',
        protocols=['openai', 'openai_responses', 'anthropic'],
        checks=[
            'system',
            'dynamic_tools',
            'dynamic_tools_both',
            'dynamic_tools_mutex',
            'dynamic_tools_role',
            'dynamic_tools_dup',
            'tool_choice_required_bare',
            'stop',
            'json_schema',
            'vision_base64',
            'cache_options_echo',
        ],
    )
    seen: list[tuple[str, str]] = []

    def fake_run_one(client, wire, api_key, check, index, should_stop=lambda: False):
        seen.append((wire.protocol, check))
        return ContractItem(index=index, check=check, title=contract.TITLES[check], protocol=wire.protocol)

    monkeypatch.setattr(contract, 'run_one', fake_run_one)
    items = contract.run_suite(settings, None)
    by_proto: dict[str, set[str]] = {'openai': set(), 'openai_responses': set(), 'anthropic': set()}
    for proto, check in seen:
        by_proto[proto].add(check)

    assert len(items) == len(seen)
    assert 'cache_options_echo' not in by_proto['openai']
    assert by_proto['openai_responses'] == {'system', 'json_schema', 'cache_options_echo'}
    assert by_proto['anthropic'] == {'system', 'stop', 'vision_base64'}


@pytest.mark.parametrize('model', ['kimi-k2.5', 'kimi-k3.1-pro', 'glm-5.3', 'gpt-4o'])
@pytest.mark.parametrize('mode', ['full', 'fast'])
def test_planned_probes_never_raise_unsupported_for_default_suites(model: str, mode: str) -> None:
    """plan() 返回的每一项 (protocol, check) 在对应默认模型族下都不应因协议或契约不适用而抛出 Unsupported。"""
    settings = ContractSettings(url='https://site.test', model=model, mode=mode)
    for protocol, check in contract.plan(settings):
        wire = contract._settings_for_protocol(settings, protocol)
        template = contract.PROBES[check]
        probe = contract.Probe(
            template.check,
            template.build,
            template.judge,
            wire_protocol=template.wire_protocol,
            supported_protocols=template.supported_protocols,
        )
        body = probe.build(wire, probe)
        assert isinstance(body, dict)


def test_cache_cold_hit_is_violated_not_washed_by_repeat() -> None:
    """P0-1: first-request cache hit must never score as conformed on the repeat."""
    settings = ContractSettings(url='https://site.test', model='kimi-k2', protocol='openai', checks=['cache'])
    warm = protocols.Reply(status=200, text='收到', usage=protocols.Usage(cached_tokens=512, prompt_tokens=2048))
    hot = protocols.Reply(status=200, text='收到', usage=protocols.Usage(cached_tokens=512, prompt_tokens=2048))
    assert contract._judge_cache(settings, hot, contract.PROBES['cache'], warm)[0] == 'violated'
    cold = protocols.Reply(status=200, text='收到', usage=protocols.Usage(cached_tokens=0, prompt_tokens=2048))
    assert contract._judge_cache(settings, hot, contract.PROBES['cache'], cold)[0] == 'conformed'


def test_cache_probe_isolates_each_build_with_uuid() -> None:
    """Without Isolation uuid, 5m TTL / per_check≥2 makes the next cold call false-red."""
    settings = ContractSettings(url='https://site.test', model='kimi-k2', protocol='openai', checks=['cache'])
    first = contract._cache(settings, contract.PROBES['cache'])
    second = contract._cache(settings, contract.PROBES['cache'])
    prompt_a = first['messages'][-1]['content'] if 'messages' in first else first.get('input')
    prompt_b = second['messages'][-1]['content'] if 'messages' in second else second.get('input')
    text_a = prompt_a if isinstance(prompt_a, str) else str(prompt_a)
    text_b = prompt_b if isinstance(prompt_b, str) else str(prompt_b)
    assert text_a != text_b
    assert text_a.startswith('Isolation ')
    assert text_b.startswith('Isolation ')


def test_system_prompt_token_inflation_flags_injection() -> None:
    """Large prompt_tokens Δ vs no-system baseline is evidence — when injection is not constant."""
    settings = ContractSettings(url='https://site.test', model='kimi-k2', protocol='openai', checks=['system'])
    baseline = protocols.Reply(status=200, text='BASELINE_OK', usage=protocols.Usage(prompt_tokens=20))
    inflated = protocols.Reply(
        status=200,
        text='嗯',
        usage=protocols.Usage(prompt_tokens=900),
        reasoning='先想想该怎么回答',
    )
    assert contract._judge_system(settings, inflated, contract.PROBES['system'], baseline)[0] == 'violated'
    ok = protocols.Reply(status=200, text='SYSTEMOK', usage=protocols.Usage(prompt_tokens=55))
    assert contract._judge_system(settings, ok, contract.PROBES['system'], baseline)[0] == 'conformed'


def test_system_injected_product_name_is_violated_even_when_delta_is_small() -> None:
    """Constant same-channel injection cancels out of Δ; identity markers still catch it."""
    settings = ContractSettings(url='https://site.test', model='kimi-k2', protocol='openai', checks=['system'])
    baseline = protocols.Reply(status=200, text='BASELINE_OK', usage=protocols.Usage(prompt_tokens=20))
    # Δ≈37 — below the inflation threshold, but reasoning leaks a vendor name we never sent.
    leaked = protocols.Reply(
        status=200,
        text='SYSTEMOK',
        usage=protocols.Usage(prompt_tokens=57),
        reasoning='作为通义千问，我应先遵守平台规则再回答 SYSTEMOK',
    )
    verdict, _, detail, observed = contract._judge_system(
        settings, leaked, contract.PROBES['system'], baseline,
    )
    assert verdict == 'violated'
    assert '通义千问' in detail
    assert 'identity=通义千问' in observed


def test_cache_tools_append_keeps_hit_after_system_tools() -> None:
    """P1-4: after a successful write, appending system.tools must still report a cache hit."""
    settings = ContractSettings(
        url='https://site.test', model='kimi-k2', protocol='openai', checks=['cache_tools_append'],
    )
    probe = contract.PROBES['cache_tools_append']
    body = contract._cache_tools_append(settings, probe)
    assert body['prompt_cache_options']['ttl'] == '5m'
    assert any(m.get('role') == 'system' and m.get('content') for m in body['messages'])

    calls: list[dict] = []

    def invoke(request):
        calls.append(request)
        if len(calls) == 1:
            return protocols.Reply(
                status=200,
                text='OK',
                usage=protocols.Usage(cache_write_tokens=512, cached_tokens=0, prompt_tokens=2048),
                headers={'msh-usage-cache-write-tokens-5m': '512'},
            )
        assert any(
            isinstance(msg, dict) and msg.get('role') == 'system' and 'tools' in msg
            for msg in request.get('messages', [])
        )
        return protocols.Reply(
            status=200,
            text='OK',
            usage=protocols.Usage(cache_write_tokens=0, cached_tokens=480, prompt_tokens=2100),
            headers={'msh-usage-cache-write-tokens-5m': '0'},
        )

    settings.cache_wait_seconds = 0
    reply, verdict = contract._run_flow(settings, probe, body, invoke, lambda: False)
    assert verdict[0] == 'conformed'
    assert reply.usage.cached_tokens == 480
    assert len(calls) == 2


def test_observation_verdict_is_excluded_from_fidelity() -> None:
    """A9: soft observation rows must not dilute the fidelity denominator."""
    items = [
        ContractItem(
            index=1, check='cache', title='缓存', verdict='conformed', severity='low',
            prompt='', expected='', observed='', detail='',
        ),
        ContractItem(
            index=2, check='cache_headers', title='缓存头', verdict='observation', severity='low',
            prompt='', expected='', observed='', detail='',
        ),
        ContractItem(
            index=3, check='tools', title='工具', verdict='inconclusive', severity='high',
            prompt='', expected='', observed='', detail='',
        ),
    ]
    summary = contract.summarize(items)
    assert summary.observation == 1
    assert summary.inconclusive == 1
    assert summary.fidelity == 100.0
    assert summary.conformed == 1
    assert summary.violated == 0


def test_auth_markers_do_not_swallow_parameter_400() -> None:
    """A7: invalid_authentication_error on HTTP 400 is still a parameter-contract signal."""
    settings = ContractSettings(url='https://site.test', model='kimi-k3.1-pro', protocol='openai')
    reply = protocols.Reply(
        status=400,
        error='HTTP 400：thinking.type disabled is not allowed',
        error_type='invalid_authentication_error',
    )
    assert contract._auth_or_capacity_status(reply) is False
    assert contract._official_client_reject(reply) is True
    # K3 locked thinking: rejecting disabled is conformed.
    assert contract._judge_thinking_disable(settings, reply, contract.PROBES['thinking_disable'])[0] == 'conformed'
    # Unofficial knobs still treat a clean parameter 400 as conformed.
    assert contract._judge_thinking_disable(
        settings, reply, contract.PROBES['thinking_disable_enable'],
    )[0] == 'conformed'


def test_anthropic_body_always_carries_max_tokens() -> None:
    """Messages rejects a body without max_tokens, so a default must be supplied."""
    settings = ContractSettings(url='https://site.test', model='m', protocol='anthropic', checks=['system'])
    assert 'max_tokens' in protocols.build_body(settings, 'hi', stream=False)
    body = protocols.build_body(settings, 'hi', stream=False, system='S', stop=['8'], max_tokens=64)
    assert body['system'] == 'S'
    assert body['stop_sequences'] == ['8']
    assert body['messages'] == [{'role': 'user', 'content': 'hi'}]


def test_gemini_uses_its_own_url_path_body_and_enum_casing() -> None:
    settings = ContractSettings(url='https://site.test', model='m', protocol='gemini', checks=['system'])
    body = protocols.build_body(
        settings,
        'hi',
        stream=False,
        system='S',
        max_tokens=32,
        response_format=protocols.json_schema_format(
            'x', {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}
        ),
    )
    assert body['systemInstruction']['parts'][0]['text'] == 'S'
    assert body['generationConfig']['maxOutputTokens'] == 32
    # Gemini wants uppercase type enums, not JSON-Schema lowercase.
    assert body['generationConfig']['responseSchema']['properties']['ok']['type'] == 'BOOLEAN'


def test_anthropic_stream_parsing_folds_deltas_and_usage() -> None:
    settings = ContractSettings(url='https://site.test', model='m', protocol='anthropic', checks=['system'])
    reply = protocols.Reply()
    for event in [
        {'type': 'message_start', 'message': {'usage': {'input_tokens': 11, 'cache_read_input_tokens': 7}}},
        {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '你'}},
        {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '好'}},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 5}},
    ]:
        protocols.parse_event(settings, event, reply)
    assert reply.text == '你好'
    assert reply.usage.cached_tokens == 7
    assert reply.usage.completion_tokens == 5
    assert reply.finish_reason == 'end_turn'


def test_gemini_blocked_prompt_surfaces_as_an_error_not_an_empty_pass() -> None:
    settings = ContractSettings(url='https://site.test', model='m', protocol='gemini', checks=['system'])
    reply = protocols.Reply()
    protocols.parse_event(settings, {'promptFeedback': {'blockReason': 'SAFETY'}}, reply)
    assert reply.error and 'SAFETY' in reply.error


def test_non_streamed_openai_body_is_read_from_message_not_delta() -> None:
    """Reading only `delta` silently produced empty text for every non-streamed call."""
    settings = ContractSettings(url='https://site.test', model='m', protocol='openai', checks=['system'])
    reply = protocols.Reply()
    protocols.parse_event(
        settings,
        {'choices': [{'message': {'content': 'hello'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 3}},
        reply,
    )
    assert reply.text == 'hello'
    assert reply.usage.prompt_tokens == 3


def test_non_streamed_responses_body_reads_output_text_and_usage() -> None:
    """Responses returns an output array for ordinary JSON responses."""
    settings = ContractSettings(url='https://site.test/v1', model='m', protocol='openai_responses', checks=['system'])
    reply = protocols.Reply()
    protocols.parse_event(
        settings,
        {
            'object': 'response',
            'status': 'completed',
            'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': 'hello'}]},
            ],
            'usage': {'input_tokens': 3, 'output_tokens': 1, 'total_tokens': 4},
        },
        reply,
    )
    assert reply.text == 'hello'
    assert reply.usage.prompt_tokens == 3
    assert reply.usage.completion_tokens == 1
    assert reply.finish_reason == 'completed'


def test_responses_non_stream_tool_call_is_normalized() -> None:
    settings = ContractSettings(url='https://site.test/v1', model='m', protocol='openai_responses', checks=['tools'])
    reply = protocols.Reply()
    protocols.parse_event(
        settings,
        {
            'object': 'response',
            'status': 'completed',
            'output': [
                {
                    'type': 'function_call',
                    'call_id': 'call_1',
                    'name': 'lookup_ticket',
                    'arguments': '{"ticket_id":"T-1024"}',
                }
            ],
        },
        reply,
    )
    assert reply.text == ''
    assert reply.tool_calls[0]['name'] == 'lookup_ticket'


def test_responses_structured_output_and_tool_choice_use_responses_shapes() -> None:
    settings = ContractSettings(url='https://site.test/v1', model='m', protocol='openai_responses', checks=['tools'])
    body = protocols.build_body(
        settings,
        'hi',
        stream=False,
        tools=[{'name': 'lookup_ticket', 'parameters': {'type': 'object'}}],
        tool_choice='required',
        response_format=protocols.json_schema_format('x', {'type': 'object'}),
    )
    assert body['tool_choice'] == 'required'
    assert body['text']['format'] == {'type': 'json_schema', 'name': 'x', 'schema': {'type': 'object'}, 'strict': True}


def test_sse_framing_lines_are_not_counted_as_content_chunks() -> None:
    """Counting blank/comment lines would make a buffered response look like a stream."""
    settings = ContractSettings(url='https://site.test', model='m', protocol='openai', checks=['stream'])
    reply = protocols.Reply()
    for line in ['', ': keep-alive', 'data: {"choices":[{"delta":{"content":"a"}}]}', '',
                 'data: {"choices":[{"delta":{"content":"b"}}]}', 'data: [DONE]']:
        if not protocols.parse_sse_line(settings, line, reply):
            break
    assert reply.chunks == 2
    assert reply.text == 'ab'


@pytest.fixture
def timed_stream(monkeypatch):
    """Deliver real HTTP stream fragments against a deterministic request clock."""
    def make(events):
        now = [0.0]
        monkeypatch.setattr(contract, 'time', SimpleNamespace(monotonic=lambda: now[0]))

        class Timeline(httpx.SyncByteStream):
            def __iter__(self):
                base = now[0]
                for elapsed_ms, payload in events:
                    now[0] = base + elapsed_ms / 1000
                    if isinstance(payload, Exception):
                        raise payload
                    data = json.dumps(payload) if isinstance(payload, dict) else payload
                    yield f'data: {data}\n\n'.encode()

        def respond(request):
            assert json.loads(request.content)['stream'] is True
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=Timeline())

        return httpx.Client(transport=httpx.MockTransport(respond))

    return make


def _text_event(text):
    return {'choices': [{'delta': {'content': text}}]}


_ROLE_EVENT = {'choices': [{'delta': {'role': 'assistant'}}]}
_FINISH_EVENT = {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}
_USAGE_EVENT = {'choices': [], 'usage': {'completion_tokens': 60}}


@pytest.mark.parametrize(
    ('events', 'expected', 'content_count', 'first_ms'),
    [
        pytest.param(
            [(1, _ROLE_EVENT), (1000, _text_event('完整答案')), (1001, _FINISH_EVENT), (2000, _USAGE_EVENT)],
            'violated', 1, 1000, id='early-role-does-not-hide-single-answer',
        ),
        pytest.param(
            [(1, _ROLE_EVENT), (1000, _FINISH_EVENT), (2000, _USAGE_EVENT)],
            'violated', 0, None, id='metadata-only-does-not-pass',
        ),
        pytest.param(
            [(100, _text_event('第一段')), (101, _text_event('第二段')), (1000, _USAGE_EVENT)],
            'violated', 2, 100, id='late-usage-does-not-extend-content-window',
        ),
        pytest.param(
            [(1000, _text_event('一')), (1000, _text_event('二')), (1000, _text_event('三'))],
            'violated', 3, 1000, id='buffered-answer-split-into-events',
        ),
        pytest.param(
            [(100, _text_event('一')), (110, _text_event('二')), (120, _text_event('三'))],
            'violated', 3, 100, id='twenty-ms-burst-boundary',
        ),
        pytest.param(
            [(1, _ROLE_EVENT), (100, _text_event('一')), (112, _text_event('二')), (124, _text_event('三')),
             (125, _FINISH_EVENT), (1000, _USAGE_EVENT), (1001, '[DONE]')],
            'conformed', 3, 100, id='progressive-content-with-late-metadata',
        ),
        pytest.param(
            [(1, _ROLE_EVENT), (1000, _text_event('一')), (1100, _text_event('二')), (1200, _text_event('三')), (1201, '[DONE]')],
            'conformed', 3, 1000, id='slow-first-content-can-still-stream',
        ),
        pytest.param(
            [(100, _text_event('一')), (200, _text_event('二')), (201, '[DONE]')],
            'violated', 2, 100, id='two-content-batches-are-insufficient',
        ),
        pytest.param(
            [(100, _text_event('一')), (100, _text_event('二')), (1000, _text_event('三')), (1001, '[DONE]')],
            'violated', 3, 100, id='one-long-gap-is-insufficient',
        ),
        pytest.param(
            [(100, _text_event('一')), (200, _text_event('二')), (300, _text_event('三')),
             (400, httpx.ReadTimeout('stream interrupted'))],
            'inconclusive', 3, 100, id='interrupted-stream-cannot-pass',
        ),
        pytest.param([], 'violated', 0, None, id='no-sse-data'),
    ],
)
def test_stream_verdict_uses_content_arrival_times(timed_stream, events, expected, content_count, first_ms) -> None:
    settings = ContractSettings(url='https://site.test', model='m', checks=['stream'])
    with timed_stream(events) as client:
        item = contract.run_one(client, settings, None, 'stream', 1)
    assert item.verdict == expected
    assert item.ttft_ms == first_ms
    assert item.observed.startswith(f'{content_count} 个有效内容块 / ')
    assert '内容跨度' in item.observed
    summary = contract.summarize([item])
    assert summary.conformed == int(expected == 'conformed')
    assert summary.violated == int(expected == 'violated')


@pytest.mark.parametrize(
    ('protocol', 'events'),
    [
        ('openai', [_ROLE_EVENT, _text_event('一'), _text_event('二'), _text_event('三'), _FINISH_EVENT, _USAGE_EVENT, '[DONE]']),
        ('openai_responses', [
            {'type': 'response.created'},
            *[{'type': 'response.output_text.delta', 'delta': text} for text in '一二三'],
            {'type': 'response.completed', 'response': {'status': 'completed', 'usage': {'output_tokens': 3}}},
        ]),
        ('anthropic', [
            {'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            *[{'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': text}} for text in '一二三'],
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 3}},
            {'type': 'message_stop'},
        ]),
        ('gemini', [
            {'usageMetadata': {'promptTokenCount': 10}},
            *[{'candidates': [{'content': {'parts': [{'text': text}]}}]} for text in '一二三'],
            {'candidates': [{'finishReason': 'STOP'}], 'usageMetadata': {'candidatesTokenCount': 3}},
        ]),
    ],
)
def test_stream_content_evidence_across_protocols(timed_stream, protocol, events) -> None:
    settings = ContractSettings(url='https://site.test', model='m', protocol=protocol, checks=['stream'])
    timeline = [(100 + index * 100, event) for index, event in enumerate(events)]
    with timed_stream(timeline) as client:
        reply = contract.call(client, settings, None, {'stream': True}, stream=True, timeout=5)
    assert reply.text == '一二三'
    assert reply.chunks == 3
    assert reply.sse_events == sum(event != '[DONE]' for event in events)
    assert len(reply.content_chunk_times_ms) == 3
    assert reply.ttft_ms == reply.content_chunk_times_ms[0]
    assert contract._judge_stream(settings, reply, contract.PROBES['stream'])[0] == 'conformed'


def test_stream_counts_reasoning_and_tool_increments_but_not_empty_metadata() -> None:
    settings = ContractSettings(url='https://site.test', model='m', protocol='anthropic', checks=['stream'])
    reply = protocols.Reply()
    events = [
        {'type': 'message_start', 'message': {}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'name': 'lookup'}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': '{}'}},
        {'type': 'content_block_delta', 'delta': {'type': 'thinking_delta', 'thinking': '先思考'}},
        {'type': 'content_block_delta', 'delta': {'type': 'thinking_delta', 'thinking': ''}},
        {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '再回答'}},
    ]
    for index, event in enumerate(events):
        protocols.parse_sse_line(settings, 'data: ' + json.dumps(event), reply, elapsed_ms=index * 100)
    # KVV: tool-call name/argument deltas also count as streamed content chunks.
    assert reply.chunks == 4
    assert reply.sse_events == 6
    assert reply.content_chunk_times_ms == [100, 200, 300, 500]
    assert reply.reasoning == '先思考'
    assert reply.text == '再回答'


def test_stream_without_per_content_timestamps_cannot_pass() -> None:
    settings = ContractSettings(url='https://site.test', model='m', checks=['stream'])
    reply = protocols.Reply(text='完整答案', chunks=3, ttft_ms=1, latency_ms=1000, stream_completed=True)
    assert contract._judge_stream(settings, reply, contract.PROBES['stream'])[0] == 'inconclusive'


def test_protocol_and_explicit_endpoint_must_still_agree() -> None:
    with pytest.raises(ValidationError):
        ContractSettings(url='https://site.test/v1/messages', model='m', protocol='openai', checks=['system'])
    with pytest.raises(ValidationError):
        ContractSettings(url='https://site.test/v1/chat/completions', model='m', protocol='anthropic', checks=['system'])


# ---------- sliding window ----------


def test_window_reports_a_trailing_rate_not_a_cumulative_total() -> None:
    window_under_test = window.TokenWindow(seconds=60, retention=300)
    for index in range(30):
        window_under_test.record(10, 10, now=1000 + index * 2)
    snapshot = window_under_test.snapshot(now=1060)
    assert snapshot['total_tpm'] == 600
    assert snapshot['samples'] == 30


def test_window_forgets_events_past_retention_so_memory_is_bounded() -> None:
    window_under_test = window.TokenWindow(seconds=60, retention=120)
    window_under_test.record(1000, 1000, now=1000)
    window_under_test.record(1, 1, now=5000)
    snapshot = window_under_test.snapshot(now=5000)
    assert snapshot['samples'] == 1
    assert snapshot['total_tpm'] == 2


def test_series_of_no_data_is_empty_rather_than_a_flat_zero_line() -> None:
    """A zero line reads as 'the site stalled' when in fact nothing was measured."""
    assert window.series([]) == []


def test_series_tracks_a_stall_that_an_average_would_hide() -> None:
    events = [window.Completion(at=1000 + index, prompt_tokens=50, completion_tokens=50) for index in range(10)]
    points = window.series(events, seconds=10, step=5)
    assert points
    assert points[0]['total_tpm'] > 0


def test_plateau_needs_a_real_tail_and_rejects_non_finite_values() -> None:
    assert window.is_plateau([1, 2, 3, 3, 3]) is True
    assert window.is_plateau([1, 2, 3]) is False
    assert window.is_plateau([1, 5, 1]) is False
    assert window.is_plateau([1, float('nan'), float('nan'), float('nan')]) is False


def test_required_tokens_scales_with_rate_and_duration() -> None:
    assert window.required_tokens(600, 60) == 600
    assert window.required_tokens(600, 30) == 300
    assert window.required_tokens(-5, 60) == 0
