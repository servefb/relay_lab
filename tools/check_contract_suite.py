"""End-to-end check of the contract suite against a controlled loopback relay.

The fixture is deliberately *hostile*: it forwards nothing faithfully. ``tools`` and
``response_format`` are stripped, ``stop`` is ignored, ``max_tokens`` is rewritten
upward, the stream is buffered into a single chunk, and history is truncated. A suite
that cannot detect all of that is worthless, so this script asserts it does.

It then runs the *same* suite against a faithful relay and asserts no false accusations.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Running this file by path puts tools/ on sys.path, not the package root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay_lab import contract  # noqa: E402
from relay_lab.config import ContractSettings  # noqa: E402

LONG_ANSWER = ' '.join(f'word{i}' for i in range(400))


class Relay(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *, faithful: bool) -> None:
        super().__init__(('127.0.0.1', 0), Handler)
        self.faithful = faithful
        self.requests = 0
        self.seen: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    server: Relay

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get('Content-Length', '0'))
        body = json.loads(self.rfile.read(length) or b'{}')
        self.server.requests += 1
        self.server.seen.append(body)
        faithful = self.server.faithful

        if body.get('model') == contract.INVALID_MODEL:
            return self._json(404, {'error': {'message': 'model not found', 'type': 'invalid_request_error'}})

        messages = body.get('messages') or []
        system = ''
        history = []
        for message in messages:
            if message.get('role') == 'system':
                system = message.get('content') or ''
            else:
                history.append(message)
        prompt = ''
        if history:
            last = history[-1].get('content')
            prompt = last if isinstance(last, str) else json.dumps(last, ensure_ascii=False)
        prompt = prompt or str(body.get('input') or '')

        # ---- hostile behaviour below ----
        tools = body.get('tools')
        if not faithful:
            tools = None
            history = history[-1:] if history else []
        schema_asked = bool(body.get('response_format') or (body.get('text') or {}).get('format'))
        if not faithful:
            # A hostile relay drops the structured-output constraint too.
            schema_asked = False
        stop = (body.get('stop') or body.get('stop_sequences')) if faithful else None
        max_tokens = body.get('max_tokens') or body.get('max_output_tokens')
        if not faithful and max_tokens:
            max_tokens = None  # ignore the cap entirely
        if not faithful:
            # And it demotes ``system`` to an ordinary user turn, which is the single
            # most common silent rewrite: the instruction still "works" for speed.
            system, prompt_extra = '', system
        else:
            prompt_extra = ''

        stream = bool(body.get('stream'))
        text = self._answer(prompt, system, history, schema_asked, stop, max_tokens, prompt_extra, faithful)
        if faithful:
            # A faithful relay actually applies the cap and the stop sequences.
            if stop:
                for word in stop:
                    if word and word in text:
                        text = text.split(word, 1)[0]
            if max_tokens and max_tokens < 200:
                text = ' '.join(text.split()[: max_tokens])
        usage = {'prompt_tokens': 20, 'completion_tokens': max(1, len(text) // 2), 'total_tokens': 20 + max(1, len(text) // 2)}
        if not faithful:
            usage = {}  # hostile relays often omit counters

        if stream:
            self._stream(text, faithful, usage, tools, prompt)
        else:
            self._single(text, faithful, usage, tools, prompt, schema_asked)

    def _answer(self, prompt, system, history, schema_asked, stop, max_tokens, prompt_extra='', faithful=True) -> str:
        """A faithful relay sends system as system; a hostile one demotes it, so the
        model then obeys the user's marker instead of the system marker."""
        if system and contract.SYSTEM_MARKER in system:
            return contract.SYSTEM_MARKER
        haystack = prompt + ' ' + str(prompt_extra)
        if contract.USER_MARKER in haystack:
            return contract.USER_MARKER
        if '481516' in str(history) and '验证码' in str(history):
            return '481516 是的，验证码是 481516。'
        if '数到 30' in haystack or '从 1 数到' in haystack:
            return '\n'.join(str(n) for n in range(1, 31))
        if schema_asked:
            return '{"model": "ThinkPad X1", "price": 1299, "in_stock": true}'
        if '山间清晨' in haystack or '河流' in haystack:
            return LONG_ANSWER
        return '好的。'

    def _single(self, text, faithful, usage, tools, prompt, schema_asked) -> None:
        if tools and ('工单' in prompt or 'T-1024' in prompt):
            payload = {
                'choices': [
                    {
                        'message': {
                            'role': 'assistant',
                            'content': None,
                            'tool_calls': [
                                {
                                    'id': 'call_1',
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
                'choices': [{'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
                'usage': usage,
            }
        self._json(200, payload)

    def _stream(self, text, faithful, usage, tools, prompt) -> None:
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        words = text.split(' ')
        if faithful:
            for word in words:
                self._event({'choices': [{'delta': {'content': word + ' '}, 'index': 0}]})
                time.sleep(0.01)
            if usage:
                self._event({'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': usage})
            self.wfile.write(b'data: [DONE]\n\n')
        else:
            # Buffered: one giant chunk, then done. This is the "fake stream" shape.
            time.sleep(0.15)
            self._event({'choices': [{'delta': {'content': text}, 'index': 0}], 'usage': usage})
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


def run_against(faithful: bool, checks: list[str]):
    relay = Relay(faithful=faithful)
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    settings = ContractSettings(
        url=f'http://127.0.0.1:{relay.server_address[1]}',
        model='sim',
        checks=checks,
        request_timeout=20,
        cache_prefix_tokens=512,
    )
    items = contract.run_suite(settings, None)
    relay.shutdown()
    relay.server_close()
    return items


def report(label: str, items) -> dict[str, str]:
    print(f'\n=== {label} ===')
    verdicts = {}
    for item in items:
        verdicts[item.check] = item.verdict
        print(f'  {item.check:12s} {item.verdict:13s} {item.detail}')
    return verdicts


def main() -> None:
    checks = ['system', 'tools', 'json_schema', 'max_tokens', 'stop', 'multi_turn', 'errors', 'stream']
    hostile = report('hostile relay (rewrites requests)', run_against(False, checks))
    faithful = report('faithful relay', run_against(True, checks))

    problems = []
    for check in ('tools', 'json_schema', 'max_tokens', 'stop', 'stream', 'multi_turn'):
        if hostile[check] != 'violated':
            problems.append(f'{check}: hostile relay was not flagged (got {hostile[check]})')
    for check, verdict in faithful.items():
        if verdict == 'violated':
            problems.append(f'{check}: faithful relay was wrongly accused')
    if faithful.get('system') == 'violated':
        problems.append('system: faithful relay was wrongly accused')
    if hostile.get('errors') != 'conformed':
        problems.append(f'errors: honest 404 was not recognised (got {hostile.get("errors")})')
    if faithful.get('stream') != 'conformed':
        problems.append(f'stream: faithful streaming was not recognised (got {faithful.get("stream")})')

    summary = contract.summarize(run_against(False, checks))
    print('\nsummary(hostile):', summary.model_dump(exclude={'by_check'}))
    if problems:
        print('\nPROBLEMS:')
        for problem in problems:
            print(' -', problem)
        raise SystemExit(1)
    print('\nOK: hostile relay fully detected, faithful relay not accused.')


if __name__ == '__main__':
    main()
