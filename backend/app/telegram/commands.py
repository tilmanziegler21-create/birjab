"""Command handling without any Telegram dependency. The bot layer forwards (user_id, text) here.

Reads go through `client.state()` (the local /api/state). The first version is read-only:
pause/halt/resume exist behind `controls_enabled=False` and are not shown in the menu unless
enabled. There is no order placement, no risk-limit editing, no liquidation, ever.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

from app.telegram import views
from app.telegram.auth import Allowlist
from app.telegram.client import StateClient, StateUnavailable

log = logging.getLogger("ats.telegram")

Button = tuple[str, str]  # (label, callback_data)

SIMPLE_MENU: list[list[Button]] = [
    [("Главная", "cmd:start"), ("Позиции", "cmd:positions")],
    [("Сделки", "cmd:trades"), ("Почему нет сделок?", "cmd:why")],
    [("AI / Новости", "cmd:news"), ("Статус", "cmd:status")],
    [("Экспертный режим", "cmd:expert")],
]
EXPERT_MENU: list[list[Button]] = [
    [("Главная", "cmd:start"), ("Позиции", "cmd:positions")],
    [("Сделки", "cmd:trades"), ("Почему нет сделок?", "cmd:why")],
    [("AI / Новости", "cmd:news"), ("Статус", "cmd:status")],
    [("A/B", "cmd:report"), ("Живая панель", "cmd:live"), ("Обновить", "cmd:refresh")],
    [("Обычный режим", "cmd:simple")],
]
READ_MENU = SIMPLE_MENU
CONTROL_ROW: list[Button] = [("⏸ Пауза", "cmd:pause"), ("▶️ Возобновить", "cmd:resume"), ("⛔ HALT", "cmd:halt")]

READ_COMMANDS = {
    "start",
    "help",
    "status",
    "balance",
    "positions",
    "trades",
    "news",
    "report",
    "whoami",
    "live",
    "why",
    "why_no_trades",
    "refresh",
    "expert",
    "simple",
}
CONTROL_COMMANDS = {"pause", "resume", "halt"}
COMMANDS = READ_COMMANDS | CONTROL_COMMANDS
CONFIRM_TTL = timedelta(seconds=60)


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    edit: bool = False


@dataclass
class PendingAction:
    action: str
    user_id: int
    created_at: datetime


class CommandService:
    def __init__(
        self,
        client: StateClient,
        allowlist: Allowlist,
        *,
        controls_enabled: bool = False,
        clock=views.utcnow,
        changes: dict | None = None,
        regimes: dict | None = None,
    ) -> None:
        self.client = client
        self.allowlist = allowlist
        self.controls_enabled = controls_enabled
        self.clock = clock
        self.changes = changes
        self.regimes = regimes
        self.dashboard = None
        self.ux_mode = "simple"
        self._pending: dict[str, PendingAction] = {}
        self.audit: list[dict[str, str]] = []

    def attach_panel(self, chat_id: int, message_id: int) -> None:
        dashboard = self.dashboard
        if dashboard is None:
            return
        dashboard.panels[str(chat_id)] = message_id
        dashboard._save()

    @property
    def expert(self) -> bool:
        dashboard = self.dashboard
        if dashboard is not None:
            return getattr(dashboard, "ux_mode", "simple") == "expert"
        return self.ux_mode == "expert"

    @property
    def menu(self) -> list[list[Button]]:
        rows = EXPERT_MENU if self.expert else SIMPLE_MENU
        return rows + ([CONTROL_ROW] if self.controls_enabled else [])

    def _set_mode(self, mode: str) -> None:
        self.ux_mode = mode
        dashboard = self.dashboard
        if dashboard is not None:
            dashboard.ux_mode = mode
            dashboard._save()

    def home_menu(self, state: dict | None = None) -> list[list[Button]]:
        rows: list[list[Button]] = []
        if state is not None:
            coins = [(symbol.split("/")[0], f"pair:{symbol}") for symbol in state.get("feed", {}).get("symbols", {})]
            if coins:
                rows.append(coins)
        return rows + self.menu

    # ------------------------------------------------------------ entry points

    def handle_text(self, user_id: int | None, text: str, *, private: bool = True) -> Reply | None:
        now = self.clock()
        command = _parse_command(text)
        if command == "whoami":
            # Works for anyone, in any chat: the id is needed to fill the allowlist. No trading data.
            return Reply(f"Ваш Telegram user id: <code>{user_id}</code>" if user_id is not None else "Не удалось определить user id.")
        if not private:
            return None  # groups and channels are ignored entirely
        if not self.allowlist.allows(user_id):
            if self.allowlist.should_reply_to_denied(user_id, now):
                return Reply("Доступ запрещён.")
            return None
        if command is None:
            return Reply("Не понял. Команды: /start /positions /trades /why /news /status /whoami", self.menu)
        return self._dispatch(command, user_id, now)  # type: ignore[arg-type]

    def handle_callback(self, user_id: int | None, data: str, *, private: bool = True) -> Reply | None:
        now = self.clock()
        if not private or not self.allowlist.allows(user_id):
            self.allowlist.should_reply_to_denied(user_id, now)
            return None
        kind, _, rest = data.partition(":")
        if kind == "cmd" and rest in COMMANDS:
            return self._dispatch(rest, user_id, now)  # type: ignore[arg-type]
        if kind == "pair":
            return self._pair_card(rest, now)
        if kind == "confirm":
            return self._confirm(rest, user_id, now)  # type: ignore[arg-type]
        if kind == "cancel":
            self._pending.pop(rest, None)
            return Reply("Отменено.", self.menu)
        return Reply("Неизвестное действие.", self.menu)

    # ------------------------------------------------------------ dispatch

    def _dispatch(self, command: str, user_id: int, now: datetime) -> Reply:
        if command in CONTROL_COMMANDS and not self.controls_enabled:
            return Reply("Эта версия бота только читает состояние. Управление (пауза/HALT) выключено настройкой.", self.menu)
        try:
            if command in CONTROL_COMMANDS:
                return self._control_command(command, user_id, now)
            state = self.client.state()
            expert = self.expert
            if command in {"expert", "simple"}:
                self._set_mode(command)
                if command == "expert":
                    return Reply("Экспертный режим включён. Служебные детали — в Статусе, A/B и Живой панели.", self.menu)
                return Reply(views.home_screen(state, now, expert=False), self.home_menu(state))
            if command in {"start", "help"}:
                return Reply(views.home_screen(state, now, expert=expert), self.home_menu(state))
            if command == "status":
                return Reply(views.status_screen(state, now, expert=expert), self.menu)
            if command == "balance":
                return Reply(views.balance_screen(state, now), self.menu)
            if command == "positions":
                return Reply(views.positions_screen(state, now, expert=expert), self.menu)
            if command == "trades":
                return Reply(views.trades_screen(state, now, expert=expert), self.menu)
            if command == "news":
                return Reply(views.news_screen(state, now, expert=expert), self.menu)
            if command == "report":
                return Reply(views.report_screen(state, now), self.menu)
            if command in {"live", "refresh"}:
                text = views.live_screen(state, now, changes=self.changes, regimes=self.regimes, last_ok_at=now)
                return Reply(text, self.menu, edit=(command == "refresh"))
            if command in {"why", "why_no_trades"}:
                return Reply(views.why_idle_screen(state, now, expert=expert), self.menu)
        except StateUnavailable as exc:
            return Reply(f"Торговый процесс недоступен по локальному API: {escape(str(exc))}. Бот работает, данных нет.", self.menu)
        except Exception as exc:  # noqa: BLE001 - a view must never take the bot down
            log.exception("telegram command failed: %s", command)
            return Reply(f"Ошибка при подготовке ответа: {escape(type(exc).__name__)}. Торговый процесс не затронут.", self.menu)
        return Reply("Неизвестная команда.", self.menu)

    def _pair_card(self, token: str, now: datetime) -> Reply:
        try:
            state = self.client.state()
        except StateUnavailable as exc:
            return Reply(f"Торговый процесс недоступен по локальному API: {escape(str(exc))}. Бот работает, данных нет.", self.menu)
        symbol = views.resolve_symbol(state, token)
        if symbol is None:
            return Reply("Нет такой монеты.", self.home_menu(state))
        back: list[list[Button]] = [[("Назад", "cmd:start")]]
        return Reply(views.pair_card_screen(state, symbol, now), back + self.home_menu(state))

    def _control_command(self, command: str, user_id: int, now: datetime) -> Reply:
        controls = self.client.state()["controls"]
        if command == "pause":
            if controls.get("halt_all"):
                return Reply("Уже действует HALT — пауза в него входит.", self.menu)
            if controls.get("pause_new_buys"):
                return Reply("Пауза покупок уже включена.", self.menu)
            self.client.control("pause")
            self._audit("pause_new_buys", user_id, now)
            return Reply("⏸ Новые покупки запрещены для обеих копий. Выходы по открытым лотам продолжают работать.", self.menu)
        if command == "resume":
            return self._ask("resume", controls, user_id, now, "▶️ Возобновить покупки?\nResume снимает только ручную паузу/HALT. Блокировки RiskManager остаются в силе.")
        return self._ask(
            "halt",
            controls,
            user_id,
            now,
            "⛔ Аварийная остановка: запрет новых покупок у обеих копий и снятие отдыхающих покупок."
            " Открытые лоты и их выходы остаются, ликвидации нет.\nПодтвердить?",
        )

    def _ask(self, action: str, controls: dict, user_id: int, now: datetime, question: str) -> Reply:
        self._expire(now)
        if action == "halt" and controls.get("halt_all"):
            return Reply("HALT уже активен.", self.menu)
        if action == "resume" and not (controls.get("pause_new_buys") or controls.get("halt_all")):
            return Reply("Ручной паузы нет — возобновлять нечего.", self.menu)
        nonce = secrets.token_urlsafe(8)
        self._pending[nonce] = PendingAction(action=action, user_id=user_id, created_at=now)
        return Reply(question, [[("Да, подтверждаю", f"confirm:{nonce}"), ("Отмена", f"cancel:{nonce}")]])

    def _confirm(self, nonce: str, user_id: int, now: datetime) -> Reply:
        self._expire(now)
        pending = self._pending.get(nonce)
        if pending is None:
            return Reply("Подтверждение уже использовано или устарело. Повторите команду.", self.menu)
        if pending.user_id != user_id:
            log.warning("telegram confirmation by another user: owner=%s actor=%s", pending.user_id, user_id)
            return Reply("Подтверждать может только тот, кто запросил действие.", self.menu)
        del self._pending[nonce]
        if not self.controls_enabled:
            return Reply("Управление выключено настройкой.", self.menu)
        try:
            controls = self.client.state()["controls"]
            if pending.action == "halt":
                if controls.get("halt_all"):
                    return Reply("HALT уже активен.", self.menu)
                self.client.control("halt")
                self._audit("halt_all", user_id, now)
                return Reply("⛔ HALT включён. Отдыхающие покупки будут сняты на ближайшем тике; выходы остаются.", self.menu)
            if pending.action == "resume":
                if not (controls.get("pause_new_buys") or controls.get("halt_all")):
                    return Reply("Ручной паузы уже нет.", self.menu)
                self.client.control("resume")
                self._audit("resume", user_id, now)
                return Reply("▶️ Ручная пауза снята. Каждая новая покупка по-прежнему проходит RiskManager.", self.menu)
        except StateUnavailable as exc:
            return Reply(f"Торговый процесс недоступен: {escape(str(exc))}. Действие не выполнено.", self.menu)
        return Reply("Неизвестное действие.", self.menu)

    # ------------------------------------------------------------ helpers

    def _expire(self, now: datetime) -> None:
        for nonce in [k for k, v in self._pending.items() if now - v.created_at > CONFIRM_TTL]:
            del self._pending[nonce]

    def _audit(self, action: str, user_id: int, now: datetime) -> None:
        self.audit.append({"action": action, "user_id": str(user_id), "at": now.isoformat()})
        log.info("telegram control action=%s user_id=%s", action, user_id)


def _parse_command(text: str) -> str | None:
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    word = text[1:].split()[0] if text[1:].split() else ""
    word = word.split("@", 1)[0].lower()
    return word if word in COMMANDS else None
