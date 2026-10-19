"""Edit one live panel message. Does not send a new message every tick."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app.telegram import views
from app.telegram.client import StateClient, StateUnavailable

log = logging.getLogger("ats.telegram")

Edit = Callable[[int, int, str], Awaitable[int | None]]
Send = Callable[[int, str], Awaitable[int | None]]


class LiveDashboard:
    def __init__(
        self,
        client: StateClient,
        *,
        state_path: Path,
        chat_ids: list[int],
        edit: Edit,
        send: Send,
        interval_sec: float = 10,
        changes: dict[str, dict[str, Any]] | None = None,
        regimes: dict[str, Any] | None = None,
        clock=views.utcnow,
    ) -> None:
        self.client = client
        self.state_path = state_path
        self.chat_ids = chat_ids
        self.edit = edit
        self.send = send
        self.interval_sec = interval_sec
        self.changes = changes if changes is not None else {}
        self.regimes = regimes if regimes is not None else {}
        self.clock = clock
        self.panels: dict[str, int] = {}
        self.last_text: dict[str, str] = {}
        self.last_ok_at: datetime | None = None
        self.ux_mode = "simple"
        self._load()

    def _load(self) -> None:
        path = self.state_path
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        live = raw.get("live")
        if isinstance(live, dict):
            self.panels = {str(k): int(v) for k, v in live.items() if str(v).lstrip("-").isdigit()}
        mode = raw.get("ux_mode")
        if mode in {"simple", "expert"}:
            self.ux_mode = mode

    def _save(self) -> None:
        path = self.state_path
        payload: dict[str, Any] = {}
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
        payload["live"] = self.panels
        payload["ux_mode"] = self.ux_mode
        payload["live_saved_at"] = views.utcnow().isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    def render(self, state: dict[str, Any], now: datetime) -> str:
        if self.ux_mode == "expert":
            return views.live_screen(state, now, changes=self.changes, regimes=self.regimes, last_ok_at=self.last_ok_at or now)
        return views.home_screen(state, now, last_ok_at=self.last_ok_at or now)

    async def ensure(self, chat_id: int, text: str) -> None:
        key = str(chat_id)
        if key in self.panels:
            return
        message_id = await self.send(chat_id, text)
        if message_id is not None:
            self.panels[key] = message_id
            self.last_text[key] = text
            self.last_ok_at = self.clock()
            self._save()

    async def push(self, *, force: bool = False) -> None:
        try:
            state = self.client.state()
        except StateUnavailable as exc:
            log.warning("live dashboard: api unavailable (%s)", exc)
            return
        now = self.clock()
        text = self.render(state, now)
        for chat_id in self.chat_ids:
            key = str(chat_id)
            message_id = self.panels.get(key)
            if message_id is None:
                await self.ensure(chat_id, text)
                continue
            if not force and self.last_text.get(key) == text:
                continue
            try:
                edited = await self.edit(chat_id, message_id, text)
            except TelegramRetryAfter as exc:
                log.warning("live dashboard RetryAfter %s", exc.retry_after)
                await asyncio.sleep(min(float(exc.retry_after), 30))
                try:
                    edited = await self.edit(chat_id, message_id, text)
                except Exception:  # noqa: BLE001
                    log.warning("live dashboard edit failed after retry", exc_info=True)
                    continue
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    self.last_text[key] = text
                    continue
                log.warning("live dashboard message gone, sending a new one")
                self.panels.pop(key, None)
                await self.ensure(chat_id, text)
                continue
            except Exception:  # noqa: BLE001
                log.warning("live dashboard edit failed", exc_info=True)
                continue
            if edited is not None:
                self.panels[key] = edited
            self.last_text[key] = text
            self.last_ok_at = now
        self._save()

    async def loop(self) -> None:
        while True:
            try:
                await self.push()
            except Exception:  # noqa: BLE001
                log.exception("live dashboard loop failed")
            await asyncio.sleep(self.interval_sec)
