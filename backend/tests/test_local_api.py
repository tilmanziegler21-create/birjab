from __future__ import annotations

import pytest

from app.config import Settings
from app.ops.local_api import is_private_http_api_url
from app.telegram.client import RemoteClient


def test_private_http_api_url_allows_loopback_and_render_hosts() -> None:
    assert is_private_http_api_url("http://127.0.0.1:8000")
    assert is_private_http_api_url("http://localhost:8000")
    assert is_private_http_api_url("http://[::1]:8000")
    assert is_private_http_api_url("http://ats-paper:10000")
    assert is_private_http_api_url("http://ats-paper-ab1c:10000")
    assert is_private_http_api_url("http://ats-paper.frankfurt.render.internal:10000")
    assert is_private_http_api_url("http://10.0.0.5:8000")
    assert is_private_http_api_url("http://192.168.1.9:8000")


def test_private_http_api_url_refuses_public_hosts() -> None:
    assert not is_private_http_api_url("http://example.com:8000")
    assert not is_private_http_api_url("https://127.0.0.1:8000")
    assert not is_private_http_api_url("http://0.0.0.0:8000")
    assert not is_private_http_api_url("http://8.8.8.8:8000")
    assert not is_private_http_api_url("http://ats-paper.onrender.com")


def test_remote_client_accepts_render_private_host_and_still_blocks_the_public_web() -> None:
    with pytest.raises(ValueError):
        RemoteClient("http://example.com:8000")
    client = RemoteClient("http://ats-paper:10000")
    assert client.base_url == "http://ats-paper:10000"
    Settings(telegram_api_base_url="http://ats-paper:10000")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Settings(telegram_api_base_url="http://0.0.0.0:8000")  # type: ignore[arg-type]
