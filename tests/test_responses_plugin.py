"""The locally added Responses API plugin: request shape and typed event parsing."""

from __future__ import annotations

import asyncio
import time

from relay_lab.vendor.perf.arguments import Arguments
from relay_lab.vendor.perf.plugin.api.responses_api import (
    ResponsesApiPlugin,
    ResponsesStream,
    error_payload_of,
    output_text_of,
    to_responses_input,
)
from relay_lab.vendor.perf.utils.benchmark_util import BenchmarkData


def plugin() -> ResponsesApiPlugin:
    return ResponsesApiPlugin(Arguments(model='m', max_tokens=16, stream=True))


def test_request_uses_input_and_max_output_tokens() -> None:
    body = plugin().build_request([{'role': 'user', 'content': 'ping'}])
    assert body['model'] == 'm'
    assert body['input'] == [{'role': 'user', 'content': 'ping'}]
    assert body['max_output_tokens'] == 16
    assert body['stream'] is True
    assert 'messages' not in body and 'max_tokens' not in body


def test_plain_string_input_stays_a_string() -> None:
    assert to_responses_input('hello') == 'hello'


def test_multimodal_parts_are_renamed_for_responses() -> None:
    messages = [{
        'role': 'user',
        'content': [
            {'type': 'text', 'text': 'what is this'},
            {'type': 'image_url', 'image_url': {'url': 'https://example.test/a.png'}},
        ],
    }]
    converted = to_responses_input(messages)
    parts = converted[0]['content']
    assert parts[0] == {'type': 'input_text', 'text': 'what is this'}
    assert parts[1] == {'type': 'input_image', 'image_url': 'https://example.test/a.png'}


def test_stream_records_ttft_once_and_maps_usage() -> None:
    output = BenchmarkData()
    started = time.perf_counter()
    stream = ResponsesStream(output, started)

    stream.feed({'type': 'response.output_text.delta', 'delta': '可'}, started + 0.10)
    stream.feed({'type': 'response.output_text.delta', 'delta': '以'}, started + 0.20)
    stream.feed({
        'type': 'response.completed',
        'response': {'usage': {'input_tokens': 11, 'output_tokens': 2,
                               'input_tokens_details': {'cached_tokens': 8}}},
    }, started + 0.25)
    stream.finish()

    assert output.generated_text == '可以'
    assert output.success is True
    assert abs(output.first_chunk_latency - 0.10) < 0.05
    assert len(output.inter_chunk_latency) == 1
    assert (output.prompt_tokens, output.completion_tokens) == (11, 2)
    assert output.real_cached_tokens == 8


def test_failed_event_becomes_an_error_not_a_success() -> None:
    output = BenchmarkData()
    stream = ResponsesStream(output, time.perf_counter())
    stream.feed({'type': 'response.failed', 'response': {'error': {'code': 'rate_limit'}}}, time.perf_counter())
    stream.finish()
    assert output.success is False
    assert 'rate_limit' in (output.error or '')


def test_non_stream_text_is_read_from_output_items() -> None:
    payload = {'output': [{'content': [{'type': 'output_text', 'text': 'positive'}]}],
               'usage': {'input_tokens': 20, 'output_tokens': 1}}
    assert output_text_of(payload) == 'positive'
    assert plugin().parse_responses([payload]) == (20, 1)


def test_http_200_error_envelope_is_not_a_success() -> None:
    payload = {'object': 'error', 'error': {'code': 'invalid_api_key', 'message': 'bad key'}}
    assert error_payload_of(payload) is not None


def test_process_request_rejects_http_200_error_envelope() -> None:
    class Response:
        status = 200
        headers = {'Content-Type': 'application/json'}
        reason = 'OK'

        async def json(self):
            return {'object': 'error', 'error': {'code': 'invalid_api_key'}}

    class Context:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *_):
            return False

    class Session:
        def post(self, **_):
            return Context()

    output = asyncio.run(plugin().process_request(Session(), 'https://site.test/v1/responses', {}, {}))
    assert output.success is False
    assert 'invalid_api_key' in (output.error or '')


def test_reasoning_delta_records_ttft_and_no_seed_in_body() -> None:
    body = ResponsesApiPlugin(Arguments(model='m', max_tokens=16, stream=True, seed=42)).build_request(
        [{'role': 'user', 'content': 'ping'}]
    )
    assert 'seed' not in body

    output = BenchmarkData()
    started = time.perf_counter()
    stream = ResponsesStream(output, started)
    stream.feed({'type': 'response.reasoning_text.delta', 'delta': '思考中'}, started + 0.15)
    stream.feed({'type': 'response.completed', 'response': {'usage': {'input_tokens': 10, 'output_tokens': 16}}}, started + 0.30)
    stream.finish()

    assert output.success is True
    assert abs(output.first_chunk_latency - 0.15) < 0.05
    assert output.generated_text == ''
