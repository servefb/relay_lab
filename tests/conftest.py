"""Shared fixtures: authenticated TestClient helpers for the JWT cookie gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay_lab.api import create_app
from relay_lab.auth import COOKIE_NAME, configured_password, configured_username


def login(client: TestClient) -> None:
    """Establish a session cookie so protected /api routes succeed."""
    response = client.post(
        '/api/auth/login',
        json={'username': configured_username(), 'password': configured_password()},
    )
    assert response.status_code == 200, response.text
    assert COOKIE_NAME in client.cookies


def authed_client(data_dir: Path, **kwargs) -> TestClient:
    client = TestClient(create_app(data_dir, schedule_loop=False, **kwargs))
    login(client)
    return client


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return authed_client(tmp_path)
