"""LLM client. Without an API key or an injected transport, no request is sent."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class LLMNotConfigured(RuntimeError):
    pass


Transport = Callable[[dict[str, str]], dict[str, Any]]


class StructuredLLMClient:
    def __init__(self, api_key: str = "", transport: Transport | None = None) -> None:
        self._api_key = api_key
        self._transport = transport

    def __repr__(self) -> str:
        return f"StructuredLLMClient(configured={bool(self._api_key)})"

    def complete_json(self, *, system: str, user: str, schema_name: str) -> dict[str, Any]:
        if not self._api_key:
            raise LLMNotConfigured("no API key; request not sent")
        if self._transport is None:
            raise LLMNotConfigured("no transport configured; request not sent")
        return self._transport({"schema_name": schema_name, "system": system, "user": user})
