"""aiogram 3 wiring: long polling, allowlist on every update, thin forwarding to CommandService.

The token is read from settings (TELEGRAM_BOT_TOKEN / ATS_TELEGRAM_BOT_TOKEN) and never logged or
echoed. In the default "process" mode this module runs in its own OS process and reaches the paper
runtime only through the local HTTP API (`RemoteClient`); it cannot import or call LiveAdapter.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import IO

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import Settings
from app.telegram.auth import Allowlist
from app.telegram.client import LocalClient, RemoteClient, StateClient, StateUnavailable
from app.telegram.commands import CommandService, Reply
from app.telegram.dashboard import LiveDashboard
from app.telegram.notify import Notifier

log = logging.getLogger("ats.telegram")

MAX_MESSAGE = 4000


def markup(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    rows = [[InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in reply.buttons]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _is_private(message: Message | None) -> bool:
    return message is not None and getattr(message.chat, "type", None) == "private"


def build_router(service: CommandService) -> Router:
    router = Router(name="ats")

    @router.message()
    async def on_message(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        reply = service.handle_text(user_id, message.text or "", private=_is_private(message))
        if reply is None:
            return
        sent = await message.answer(reply.text[:MAX_MESSAGE], reply_markup=markup(reply))
        command = (message.text or "").split()[0].split("@", 1)[0].lower()
        if command in {"/live", "/refresh"} and sent is not None:
            service.attach_panel(message.chat.id, sent.message_id)

    @router.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        user_id = query.from_user.id if query.from_user else None
        message = query.message if isinstance(query.message, Message) else None
        reply = service.handle_callback(user_id, query.data or "", private=_is_private(message))
        try:
            await query.answer()
        except Exception:  # noqa: BLE001 - stale callbacks are not worth a crash
            log.debug("callback ack failed", exc_info=True)
        if reply is None or message is None:
            return
        body = reply.text[:MAX_MESSAGE]
        keyboard = markup(reply)
        if reply.edit:
            try:
                await message.edit_text(body, reply_markup=keyboard)
                service.attach_panel(message.chat.id, message.message_id)
                return
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(exc.retry_after, 30))
                try:
                    await message.edit_text(body, reply_markup=keyboard)
                    service.attach_panel(message.chat.id, message.message_id)
                    return
                except Exception:  # noqa: BLE001
                    log.debug("edit after retry failed", exc_info=True)
            except Exception:  # noqa: BLE001
                log.debug("edit_text failed; sending a new message", exc_info=True)
        sent = await message.answer(body, reply_markup=keyboard)
        if (query.data or "").startswith("cmd:live") and sent is not None:
            service.attach_panel(message.chat.id, sent.message_id)

    return router


def build_dispatcher(service: CommandService) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(service))
    return dispatcher


def make_bot(token: str, session: BaseSession | None = None) -> Bot:
    return Bot(token=token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def make_sender(bot: Bot, chat_ids: list[int]) -> Callable[[str], Awaitable[None]]:
    async def send(text: str) -> None:
        for chat_id in chat_ids:
            try:
                await bot.send_message(chat_id, text[:MAX_MESSAGE])
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(exc.retry_after, 30))
                await bot.send_message(chat_id, text[:MAX_MESSAGE])

    return send


def notification_targets(settings: Settings) -> list[int]:
    raw = settings.telegram_chat_id.strip()
    if raw and raw.lstrip("-").isdigit():
        return [int(raw)]
    return sorted(settings.telegram_user_ids)


class PollingLock:
    """One polling instance per token: an exclusive flock on var/telegram.<token-hash>.lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - held for the process lifetime
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return True

    def unlock(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def lock_path_for(settings: Settings) -> Path:
    import hashlib

    digest = hashlib.sha256(settings.telegram_bot_token.encode("utf-8")).hexdigest()[:12]
    return Path(settings.telegram_state_path).parent / f"telegram.{digest}.lock"


class TelegramApp:
    """Runs polling and the notifier side by side. Works over any StateClient."""

    def __init__(self, source: StateClient | object, settings: Settings, *, session: BaseSession | None = None) -> None:
        if not settings.telegram_enabled:
            raise ValueError("telegram requires TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS")
        self.settings = settings
        self.client: StateClient = source if hasattr(source, "state") and hasattr(source, "control") else LocalClient(source)  # type: ignore[assignment]
        self.allowlist = Allowlist(settings.telegram_user_ids)
        self.changes: dict = {}
        self.regimes: dict = {}
        self.service = CommandService(
            self.client,
            self.allowlist,
            controls_enabled=settings.telegram_controls_enabled,
            changes=self.changes,
            regimes=self.regimes,
        )
        self.bot = make_bot(settings.telegram_bot_token, session)
        self.dispatcher = build_dispatcher(self.service)
        self.sender = make_sender(self.bot, notification_targets(settings))
        self.notifier = Notifier(
            self.client,
            self.sender,
            max_per_minute=settings.telegram_max_messages_per_minute,
            digest_interval_sec=settings.telegram_digest_interval_sec,
            drawdown_alert_pct=settings.telegram_drawdown_alert_pct,
            state_path=Path(settings.telegram_state_path),
        )
        self.dashboard = LiveDashboard(
            self.client,
            state_path=Path(settings.telegram_state_path),
            chat_ids=notification_targets(settings),
            edit=self._edit_panel,
            send=self._send_panel,
            interval_sec=10,
            changes=self.changes,
            regimes=self.regimes,
        )
        self.service.dashboard = self.dashboard
        self.poll_interval_sec = 2.0

    def startup_text(self) -> str:
        state = self.notifier.last_state
        if state is None:
            return "🤖 Telegram-наблюдатель запущен · PAPER · live отключён.\nТорговый процесс пока недоступен по локальному API."
        arms = state["arms"]
        return (
            "Telegram подключён к торговому процессу. PAPER, live отключён, только чтение.\n"
            f"Активных сделок {arms['baseline']['open_lots']}, закрытых {arms['baseline']['completed_cycles']}. /start — главное меню."
        )

    async def send_test(self) -> None:
        """One real message to the configured targets. Used to verify the connection explicitly."""

        self.notifier.prime()
        await self.sender("✅ Тестовое уведомление: бот видит paper-процесс по локальному API.\n" + self.startup_text())

    async def run(self) -> None:
        self.notifier.prime()
        try:
            await self.notifier.announce(self.startup_text())
        except Exception:  # noqa: BLE001
            log.exception("telegram announce failed")
        notifier_task = asyncio.create_task(self._notify_loop(), name="ats-telegram-notify")
        dashboard_task = asyncio.create_task(self.dashboard.loop(), name="ats-telegram-live")
        cache_task = asyncio.create_task(self._kline_loop(), name="ats-telegram-klines")
        try:
            await self.dispatcher.start_polling(self.bot, handle_signals=False, allowed_updates=["message", "callback_query"])
        finally:
            for task in (cache_task, dashboard_task, notifier_task):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await self.bot.session.close()

    def _panel_menu(self) -> list:
        try:
            return self.service.home_menu(self.client.state())
        except StateUnavailable:
            return self.service.menu

    async def _edit_panel(self, chat_id: int, message_id: int, text: str) -> int | None:
        keyboard = markup(Reply(text, self._panel_menu()))
        await self.bot.edit_message_text(text[:MAX_MESSAGE], chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
        return message_id

    async def _send_panel(self, chat_id: int, text: str) -> int | None:
        keyboard = markup(Reply(text, self._panel_menu()))
        try:
            sent = await self.bot.send_message(chat_id, text[:MAX_MESSAGE], reply_markup=keyboard)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(exc.retry_after, 30))
            sent = await self.bot.send_message(chat_id, text[:MAX_MESSAGE], reply_markup=keyboard)
        return sent.message_id

    async def _kline_loop(self) -> None:
        from app.market.http_get import read_only_get
        from app.market.klines_cache import KlineCache
        from app.market.public import PublicBookClient
        from app.market.regime import default_store

        cache = KlineCache(PublicBookClient(read_only_get), store_path=default_store())
        while True:
            try:
                state = self.client.state()
                symbols = list(state["feed"]["symbols"])
                quote = str(state.get("quote_currency") or "USDT")
                cache.maybe_refresh(symbols, quote)
                self.changes.clear()
                self.changes.update(cache.changes)
                self.regimes.clear()
                self.regimes.update(cache.regimes)
            except Exception:  # noqa: BLE001
                log.debug("kline cache refresh failed", exc_info=True)
            await asyncio.sleep(60)

    async def _notify_loop(self) -> None:
        while True:
            try:
                await self.notifier.poll()
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("telegram notifier loop failed")
            await asyncio.sleep(self.poll_interval_sec)


def build_remote_app(settings: Settings) -> TelegramApp:
    return TelegramApp(RemoteClient(settings.telegram_api_base_url), settings)
