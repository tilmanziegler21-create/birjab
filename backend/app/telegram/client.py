"""How the Telegram process reaches the paper runtime: the local HTTP API, nothing else.

`RemoteClient` speaks to loopback or a private Render host with GET /api/state and, only when
controls are enabled, POST /api/controls/{pause,halt,resume}. There is no order endpoint to call.
`LocalClient` wraps a runtime object for tests.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

from app.ops.local_api import is_private_http_api_url


class StateUnavailable(RuntimeError):
    pass


class StateClient(Protocol):
    def state(self) -> dict[str, Any]: ...

    def control(self, action: str) -> dict[str, Any]: ...


ALLOWED_CONTROLS = {"pause", "halt", "resume"}

Transport = Callable[[str, str, bytes | None], bytes]


def _urllib_transport(method: str, url: str, body: bytes | None) -> bytes:
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise StateUnavailable(f"HTTP {exc.code} for {method} {url.split('/api/', 1)[-1]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise StateUnavailable(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from exc


class RemoteClient:
    def __init__(self, base_url: str, *, transport: Transport | None = None) -> None:
        if not is_private_http_api_url(base_url):
            raise ValueError("telegram talks to the paper API on loopback or a private host only")
        self.base_url = base_url.rstrip("/")
        self._transport = transport or _urllib_transport

    def state(self) -> dict[str, Any]:
        raw = self._transport("GET", f"{self.base_url}/api/state", None)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StateUnavailable("invalid json from api") from exc
        if not isinstance(payload, dict) or "arms" not in payload:
            raise StateUnavailable("unexpected state payload")
        return payload

    def health(self) -> dict[str, Any]:
        return json.loads(self._transport("GET", f"{self.base_url}/health", None))

    def control(self, action: str) -> dict[str, Any]:
        if action not in ALLOWED_CONTROLS:
            raise ValueError(f"control not permitted from telegram: {action}")
        raw = self._transport("POST", f"{self.base_url}/api/controls/{action}", b"{}")
        return json.loads(raw)


class LocalClient:
    """In-process adapter over a PaperRuntime. Used by tests; production uses RemoteClient."""

    def __init__(self, runtime: Any, *, clock: Callable[[], Any] | None = None) -> None:
        self.runtime = runtime
        self.clock = clock

    def state(self) -> dict[str, Any]:
        return self.runtime.snapshot(self.clock()) if self.clock is not None else self.runtime.snapshot()

    def control(self, action: str) -> dict[str, Any]:
        if action not in ALLOWED_CONTROLS:
            raise ValueError(f"control not permitted from telegram: {action}")
        getattr(self.runtime, action)()
        return {"controls": self.runtime.snapshot()["controls"]}
