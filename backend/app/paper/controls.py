"""Manual switches. PAUSE is one click; STOP & LIQUIDATE needs the confirmation string."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.risk.emergency import CONFIRM_LIQUIDATE


@dataclass
class Controls:
    pause_new_buys: bool = False
    halt_all: bool = False
    liquidate_confirmed: bool = False
    history: list[dict[str, str]] = field(default_factory=list)

    def pause(self, now: datetime) -> None:
        self.pause_new_buys = True
        self._log("pause_new_buys", now)

    def halt(self, now: datetime) -> None:
        self.pause_new_buys = True
        self.halt_all = True
        self._log("halt_all", now)

    def resume(self, now: datetime) -> None:
        self.pause_new_buys = False
        self.halt_all = False
        self.liquidate_confirmed = False
        self._log("resume", now)

    def request_liquidate(self, confirmation: str, now: datetime) -> bool:
        if confirmation != CONFIRM_LIQUIDATE:
            self._log("liquidate_refused", now)
            return False
        self.pause_new_buys = True
        self.halt_all = True
        self.liquidate_confirmed = True
        self._log("liquidate_confirmed", now)
        return True

    def _log(self, action: str, now: datetime) -> None:
        self.history.append({"action": action, "at": now.isoformat()})
