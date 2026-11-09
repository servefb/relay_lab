"""Loopback-only SSE fixture for the zero-cost demo button.

It never proxies traffic: short replies get eight slots, long generations get
one, so a concurrency ladder is guaranteed to discover a boundary on this box.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class DemoServer(ThreadingHTTPServer):
    """Emulate cheap short replies and one long-generation slot."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(('127.0.0.1', 0), _DemoHandler)
        self.active = 0
        self.requests_seen = 0
        self.guard = threading.Lock()


class _DemoHandler(BaseHTTPRequestHandler):
    server: DemoServer

    def log_message(self, format: str, *args: Any) -> None:
        """Do not log request bodies, credentials or client metadata."""

    def do_POST(self) -> None:
        """Serve two finite streaming shapes and an intentional 429 condition."""
        if self.path not in ('/v1/chat/completions', '/v1/responses'):
            self.send_error(501)
            return
        body = self.rfile.read(min(int(self.headers.get('Content-Length', '0')), 65536))
        try:
            payload = json.loads(body)
        except ValueError:
            self.send_error(400)
            return
        with self.server.guard:
            self.server.requests_seen += 1
            output_limit = payload.get('max_tokens', payload.get('max_output_tokens', 512))
            slots = 8 if output_limit <= 64 else 1
            busy = self.server.active >= slots
            if not busy:
                self.server.active += 1
        if busy:
            self.send_response(429)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"Local demo: one generation slot","type":"rate_limit"}}')
            return
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            responses = self.path == '/v1/responses'
            if responses:
                self._event({'type': 'response.created', 'response': {'id': 'demo', 'status': 'in_progress'}})
            else:
                self._event({'choices': [{'delta': {'role': 'assistant'}, 'index': 0}]})
            time.sleep(0.035)
            for index in range(16):
                if responses:
                    self._event({'type': 'response.output_text.delta', 'delta': f'word{index} '})
                else:
                    self._event({'choices': [{'delta': {'content': f'word{index} '}, 'index': 0}]})
                time.sleep(0.008)
            if responses:
                self._event(
                    {
                        'type': 'response.completed',
                        'response': {
                            'id': 'demo',
                            'status': 'completed',
                            'output': [],
                            'usage': {'input_tokens': 24, 'output_tokens': 16, 'total_tokens': 40},
                        },
                    }
                )
            else:
                self._event(
                    {
                        'choices': [{'delta': {}, 'finish_reason': 'stop', 'index': 0}],
                        'usage': {'prompt_tokens': 24, 'completion_tokens': 16, 'total_tokens': 40},
                    }
                )
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self.server.guard:
                self.server.active -= 1

    def _event(self, value: dict) -> None:
        self.wfile.write(('data: ' + json.dumps(value) + '\n\n').encode())
        self.wfile.flush()
