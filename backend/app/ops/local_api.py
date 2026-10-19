"""Which HTTP URLs the Telegram process may call for paper state.

Loopback stays the default. On Render the bot talks to the private service over
the internal network (service hostname, no public DNS). Public hosts are refused.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def normalize_http_api_url(url: str) -> str:
    """Render hostport is `service:10000` with no scheme. Telegram settings need http://."""

    cleaned = (url or "").strip()
    if cleaned and "://" not in cleaned:
        return f"http://{cleaned}"
    return cleaned


def is_private_http_api_url(url: str) -> bool:
    parsed = urlparse(normalize_http_api_url(url))
    if parsed.scheme != "http":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    if host.endswith(".render.internal") and _labels_ok(host):
        return True
    if "." not in host:
        return _labels_ok(host)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return False
    return bool(ip.is_loopback or ip.is_private)


def _labels_ok(host: str) -> bool:
    parts = host.split(".")
    return bool(parts) and all(part and part.replace("-", "").isalnum() for part in parts)
