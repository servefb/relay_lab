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

        if body.get('model') == contract.INVALID_MODEL:
            self._json(404, {'error': {'message': 'model not found'}})
            return

        messages = body.get('messages') or []
        system = next((m.get('content') or '' for m in messages if m.get('role') == 'system'), '')
        history = [m for m in messages if m.get('role') != 'system']
        last = history[-1].get('content') if history else ''
        prompt = last if isinstance(last, str) else json.dumps(last, ensure_ascii=False)
        prompt = prompt or str(body.get('input') or '')

        tools = body.get('tools')
        schema = bool(body.get('response_format') or (body.get('text') or {}).get('format'))
        stop = body.get('stop') or body.get('stop_sequences')
        cap = body.get('max_tokens') or body.get('max_output_tokens')
        if not faithful:
            tools, schema, stop, cap = None, False, None, None

        text = self._answer(prompt, system, history, schema, faithful)
        if faithful and stop:
            for word in stop:
                if word and word in text:
                    text = text.split(word, 1)[0]
        if faithful and cap and cap < 200:
            text = ' '.join(text.split()[:cap])

        usage = {'prompt_tokens': 20, 'completion_tokens': max(1, len(text) // 2)}
        if not faithful:
            usage = {}
        if body.get('stream'):
            self._stream(text, faithful, usage)
        else:
            self._single(text, usage, tools, prompt, schema)

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

    def _single(self, text, usage, tools, prompt, schema) -> None:
        if tools and ('工单' in prompt or 'T-1024' in prompt):
            payload = {
                'choices': [
                    {
                        'message': {
                            'content': None,
                            'tool_calls': [
                                {
                                    'id': 'c1',
                                    'type': 'function',
                                    'function': {'name': contract.TOOL_NAME, 'arguments': '{"ticket_id":"T-1024"}'},
                                }
                            ],
                        },
                        'finish_reason': 'tool_calls',
                    }
                ],
                'usage': usage,
            }
        else:
            payload = {
                'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
                'usage': usage,
            }
        self._json(200, payload)

    def _stream(self, text, faithful, usage) -> None:
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if faithful:
            for word in text.split(' '):
                self._event({'choices': [{'delta': {'content': word + ' '}}]})
                time.sleep(0.008)
        else:
            time.sleep(0.12)
            self._event({'choices': [{'delta': {'content': text}}], 'usage': usage})
        self.wfile.write(b'data: [DONE]\n\n')
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


def verdicts(items: list[ContractItem]) -> dict[str, str]:
    return {item.check: item.verdict for item in items}


CHECKS = ['system', 'tools', 'json_schema', 'max_tokens', 'stop', 'multi_turn', 'errors', 'stream']


# ---------- the suite must detect tampering ----------


def test_tampering_relay_is_flagged_on_every_high_severity_check(tampering) -> None:
    got = verdicts(contract.run_suite(settings_for(tampering, CHECKS), None))
    for check in ('tools', 'json_schema', 'max_tokens', 'stop', 'stream'):
        assert got[check] == 'violated', f'{check} was not flagged: {got[check]}'


def test_tampering_relay_keeps_a_honest_404_as_conformed(tampering) -> None:
    """The error probe's subject *is* the failure, so a real 404 must count as a pass."""
    summary = contract.summarize(contract.run_suite(settings_for(tampering, ['errors']), None))
    assert summary.by_check['errors'].conformed == 1
    assert summary.by_check['errors'].violated == 0


# ---------- and must not accuse an honest relay ----------


def test_honest_relay_is_never_accused(honest) -> None:
    items = contract.run_suite(settings_for(honest, CHECKS), None)
    wrongly = [item.check for item in items if item.verdict == 'violated']
    assert wrongly == [], f'false accusation on {wrongly}'
    assert verdicts(items)['stream'] == 'conformed'
    assert verdicts(items)['system'] == 'conformed'


def test_honest_relay_cache_without_counters_is_inconclusive(honest) -> None:
    """Silence about caching must never be read as a cache violation."""
    summary = contract.summarize(contract.run_suite(settings_for(honest, ['cache']), None))
    assert summary.violated == 0
    assert summary.inconclusive == 1


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
