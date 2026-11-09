"""Model discovery: one GET, sanitised errors, bounded response."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from relay_lab.config import ModelsRequest
from relay_lab.discovery import DiscoveryError, fetch_models, models_endpoint


class _Handler(BaseHTTPRequestHandler):
    status = 200
    body: bytes = b'{}'
    seen: list[str] = []

    def log_message(self, format: str, *args) -> None:
        """Keep test output clean."""

    def do_GET(self) -> None:
        _Handler.seen.append(self.path)
        self.send_response(_Handler.status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(_Handler.body)


@pytest.fixture()
def upstream():
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Handler.status, _Handler.body, _Handler.seen = 200, b'{}', []
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize(
    ('address', 'expected'),
    [
        ('https://site.example', 'https://site.example/v1/models'),
        ('https://site.example/v1', 'https://site.example/v1/models'),
        ('https://site.example/v1/chat/completions', 'https://site.example/v1/models'),
        ('https://site.example/openai/v1/responses', 'https://site.example/openai/v1/models'),
        ('https://site.example/v1/models', 'https://site.example/v1/models'),
    ],
)
def test_endpoint_mapping_keeps_proxy_prefixes(address: str, expected: str) -> None:
    assert models_endpoint(address) == expected


def test_valid_list_is_sorted_and_deduplicated(upstream: str) -> None:
    _Handler.body = json.dumps({'data': [{'id': 'b'}, {'id': 'a'}, {'id': 'b'}, {'nope': 1}, {'id': ' '}]}).encode()
    response = fetch_models(ModelsRequest(url=upstream))
    assert response.models == ['a', 'b']
    assert response.endpoint.endswith('/v1/models')
    assert response.truncated is False


def test_http_errors_are_explained_without_echoing_the_body(upstream: str) -> None:
    _Handler.status = 401
    _Handler.body = b'{"error":"secret-ish upstream detail"}'
    with pytest.raises(DiscoveryError) as error:
        fetch_models(ModelsRequest(url=upstream))
    assert 'API Key' in str(error.value)
    assert 'secret-ish' not in str(error.value)


def test_incompatible_payloads_are_reported(upstream: str) -> None:
    _Handler.body = b'<html>hello</html>'
    with pytest.raises(DiscoveryError) as error:
        fetch_models(ModelsRequest(url=upstream))
    assert 'JSON' in str(error.value)


def test_redirects_are_refused_so_the_key_is_not_forwarded(upstream: str) -> None:
    _Handler.status = 302
    with pytest.raises(DiscoveryError) as error:
        fetch_models(ModelsRequest(url=upstream))
    assert '重定向' in str(error.value)
