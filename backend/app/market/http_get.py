"""One public GET. No API key header and no order URL."""

from __future__ import annotations

import ssl
from collections.abc import Callable
from urllib.request import Request, urlopen

from app.market.archive import ArchiveError, assert_archive_url
from app.market.public import PublicBookError, assert_public_read_url


def _ssl_context() -> ssl.SSLContext:
    """Verified TLS. The certifi bundle is used when present (python.org macOS builds ship none)."""

    try:
        import certifi
    except ImportError:  # pragma: no cover - certifi is a declared dependency
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def read_only_get(
    url: str,
    *,
    opener: Callable[..., object] | None = None,
    timeout: float = 5,
) -> bytes:
    assert_public_read_url(url)
    request = Request(url, method="GET")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "ai-trading-system/paper-readonly")
    try:
        if opener is None:
            with urlopen(request, timeout=timeout, context=_ssl_context()) as response:
                body = response.read()
        else:
            with opener(request, timeout=timeout) as response:  # type: ignore[operator]
                body = response.read()
    except PublicBookError:
        raise
    except Exception as exc:
        raise PublicBookError("public_get_failed") from exc
    if not isinstance(body, bytes):
        raise PublicBookError("body_not_bytes")
    return body


def archive_get(
    url: str,
    *,
    opener: Callable[..., object] | None = None,
    timeout: float = 60,
) -> bytes:
    """GET a Binance Vision zip. Separate allowlist from the public trading API."""

    assert_archive_url(url)
    request = Request(url, method="GET")
    request.add_header("User-Agent", "ai-trading-system/paper-readonly")
    try:
        if opener is None:
            with urlopen(request, timeout=timeout, context=_ssl_context()) as response:
                body = response.read()
        else:
            with opener(request, timeout=timeout) as response:  # type: ignore[operator]
                body = response.read()
    except ArchiveError:
        raise
    except Exception as exc:
        raise ArchiveError("archive_get_failed") from exc
    if not isinstance(body, bytes) or len(body) < 4 or body[:2] != b"PK":
        raise ArchiveError("not_a_zip")
    return body
