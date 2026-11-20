# Vendored EvalScope perf engine lacks a Responses API plugin; this is a local addition.
"""OpenAI Responses API (`POST /responses`) for the vendored load generator.

The engine speaks Chat Completions internally: datasets hand over chat messages, and
metrics expect `choices[]` deltas. The Responses API is different in three places this
module adapts:

* the request body uses ``input`` and ``max_output_tokens`` instead of ``messages`` and
  ``max_tokens``;
* streaming is a typed event stream (``response.output_text.delta`` /
  ``response.completed``) rather than one uniform ``choices`` chunk;
* usage is reported as ``input_tokens`` / ``output_tokens`` on the final response.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from typing import Any, Dict, List, Tuple, Union

from relay_lab.vendor.perf.arguments import Arguments
from relay_lab.vendor.perf.plugin.api.default_api import DefaultApiPlugin, StreamedResponseHandler
from relay_lab.vendor.perf.plugin.registry import register_api
from relay_lab.vendor.perf.utils.benchmark_util import BenchmarkData, is_stream_body
from relay_lab.vendor.support.logger import get_logger

logger = get_logger()

TEXT_DELTA_EVENTS = ('response.output_text.delta',)
REASONING_DELTA_EVENTS = (
    'response.reasoning_summary_text.delta',
    'response.reasoning_text.delta',
    'response.reasoning.delta',
)
COMPLETION_EVENTS = ('response.completed', 'response.incomplete')
FAILURE_EVENTS = ('response.failed', 'response.error', 'error')


def to_responses_input(messages: Union[List[Dict], str, Any]) -> Union[str, List[Dict]]:
    """Translate chat-style messages into the Responses ``input`` shape.

    A plain string is already valid. Message content parts are renamed: the Responses
    API calls them ``input_text`` / ``input_image``, and an image URL is a bare string
    rather than the nested ``{"url": ...}`` object Chat Completions expects.
    """
    if not isinstance(messages, list):
        return messages
    converted: List[Dict] = []
    for message in messages:
        if not isinstance(message, dict):
            return messages
        role = message.get('role', 'user')
        content = message.get('content')
        if not isinstance(content, list):
            converted.append({'role': role, 'content': content})
            continue
        parts: List[Dict] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get('type')
            if kind in ('text', 'input_text'):
                parts.append({'type': 'input_text', 'text': part.get('text', '')})
            elif kind in ('image_url', 'input_image'):
                image = part.get('image_url', part.get('image_url'))
                if isinstance(image, dict):
                    image = image.get('url')
                parts.append({'type': 'input_image', 'image_url': image})
            else:
                parts.append(part)
        converted.append({'role': role, 'content': parts})
    return converted


def output_text_of(payload: Dict) -> str:
    """Concatenate text from a non-streaming Responses payload."""
    pieces: List[str] = []
    for item in payload.get('output') or []:
        if not isinstance(item, dict):
            continue
        for part in item.get('content') or []:
            if isinstance(part, dict) and part.get('type') in ('output_text', 'text'):
                pieces.append(part.get('text') or '')
    return ''.join(pieces)


def error_payload_of(payload: Dict) -> str | None:
    """Return a readable error for a HTTP 200 Responses error envelope."""
    if not isinstance(payload, dict):
        return None
    if payload.get('error') is not None or payload.get('object') == 'error' or payload.get('status') == 'failed':
        return json.dumps(payload, ensure_ascii=False)
    return None


class ResponsesStream:
    """Accumulate typed Responses events into a BenchmarkData record."""

    def __init__(self, output: BenchmarkData, started: float) -> None:
        self.output = output
        self.started = started
        self.text = ''
        self.last_timestamp: float | None = None
        self.first_seen = False

    def feed(self, payload: Dict, timestamp: float) -> None:
        event = payload.get('type')
        if event in TEXT_DELTA_EVENTS or event in REASONING_DELTA_EVENTS:
            text = payload.get('delta') or ''
            if not text:
                return
            if not self.first_seen:
                # 思考模型在输出正文前会先输出 reasoning delta，同样属于首包延迟。
                self.output.first_chunk_latency = timestamp - self.started
                self.first_seen = True
            elif self.last_timestamp is not None:
                self.output.inter_chunk_latency.append(timestamp - self.last_timestamp)
            self.last_timestamp = timestamp
            self.output.n_decode_chunks += 1
            if event in TEXT_DELTA_EVENTS:
                self.text += text
            return
        if event in COMPLETION_EVENTS:
            response = payload.get('response') or {}
            usage = response.get('usage') or {}
            self.output.prompt_tokens = usage.get('input_tokens')
            self.output.completion_tokens = usage.get('output_tokens')
            details = usage.get('input_tokens_details')
            if isinstance(details, dict) and details.get('cached_tokens') is not None:
                self.output.real_cached_tokens = details['cached_tokens']
            if isinstance(response, dict) and response:
                self.output.response_messages.append(response)
            self.last_timestamp = timestamp
            return
        if event in FAILURE_EVENTS:
            detail = payload.get('response') if isinstance(payload.get('response'), dict) else payload
            error = detail.get('error') if isinstance(detail, dict) else detail
            self.output.error = json.dumps(error if error is not None else payload, ensure_ascii=False)

    def finish(self) -> None:
        self.output.generated_text = self.text
        self.output.success = self.output.error is None
        self.output.completed_time = self.last_timestamp or time.perf_counter()
        self.output.query_latency = self.output.completed_time - self.started


@register_api('openai_responses')
class ResponsesApiPlugin(DefaultApiPlugin):
    """Load-generator plugin for the OpenAI Responses API."""

    def build_request(self, messages: Union[List[Dict], str], param: Arguments = None) -> Dict:
        param = param or self.param
        body: Dict[str, Any] = {
            'model': param.model,
            'input': to_responses_input(messages),
            'stream': bool(param.stream),
        }
        if param.max_tokens is not None:
            body['max_output_tokens'] = param.max_tokens
        for key, value in (
            ('temperature', param.temperature),
            ('top_p', param.top_p),
        ):
            if value is not None:
                body[key] = value
        if param.extra_args:
            body.update(param.extra_args)
        return body

    def set_request_max_tokens(self, request: Dict, max_tokens: int) -> None:
        request['max_output_tokens'] = max_tokens

    async def process_request(self, client_session, url: str, headers: Dict, body: Dict) -> BenchmarkData:
        headers = {'Content-Type': 'application/json', **headers}
        if isinstance(body, dict) and body.get('stream') is True:
            headers.setdefault('Accept', 'text/event-stream')
        data = json.dumps(body, ensure_ascii=False)
        output = BenchmarkData()
        output.request = data
        started = time.perf_counter()
        output.start_time = started
        first_byte: float | None = None
        try:
            async with client_session.post(url=url, data=data, headers=headers) as response:
                content_type = response.headers.get('Content-Type', '')
                if response.status == 200:
                    if 'text/event-stream' in content_type:
                        output.is_stream = True
                        stream = ResponsesStream(output, started)
                        handler = StreamedResponseHandler()
                        async for chunk_bytes in response.content.iter_any():
                            if not chunk_bytes:
                                continue
                            if first_byte is None:
                                first_byte = time.perf_counter() - started
                            for message in handler.add_chunk(chunk_bytes):
                                if message.startswith(':'):
                                    continue
                                chunk = message.removeprefix('data:').strip()
                                if not chunk or chunk == '[DONE]':
                                    continue
                                try:
                                    payload = json.loads(chunk)
                                except json.JSONDecodeError:
                                    continue
                                stream.feed(payload, time.perf_counter())
                        stream.finish()
                        if output.first_chunk_latency <= 0 and first_byte is not None:
                            output.first_chunk_latency = first_byte
                    else:
                        payload = await response.json()
                        timestamp = time.perf_counter()
                        output.completed_time = timestamp
                        output.query_latency = timestamp - started
                        output.first_chunk_latency = output.query_latency
                        if isinstance(payload, dict):
                            output.error = error_payload_of(payload)
                            if output.error is None:
                                output.generated_text = output_text_of(payload)
                                usage = payload.get('usage') or {}
                                output.prompt_tokens = usage.get('input_tokens')
                                output.completion_tokens = usage.get('output_tokens')
                                output.response_messages.append(payload)
                        else:
                            output.generated_text = str(payload)
                        output.success = output.error is None
                else:
                    output.status_code = response.status
                    try:
                        output.error = json.dumps(await response.json(), ensure_ascii=False)
                    except Exception:
                        try:
                            output.error = await response.text()
                        except Exception:
                            output.error = response.reason or ''
                    output.success = False
        except Exception:
            output.success = False
            output.error = ''.join(traceback.format_exception(*sys.exc_info()))
            logger.error(output.error)

        if not output.success and not output.is_stream:
            output.is_stream = is_stream_body(body)
        return output

    def parse_responses(self, responses: List[Dict], request: str = None, **kwargs: Any) -> Tuple[int, int]:
        """Read token counts from the final response's usage block."""
        for payload in reversed(responses):
            if not isinstance(payload, dict):
                continue
            usage = payload.get('usage')
            if isinstance(usage, dict):
                return int(usage.get('input_tokens') or 0), int(usage.get('output_tokens') or 0)
        return 0, 0
