"""Allowlist of Telegram user ids. Every update is checked; nothing else identifies a user."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

log = logging.getLogger("ats.telegram")


@dataclass
class Allowlist:
    user_ids: frozenset[int]
    deny_reply_cooldown_sec: int = 3600
    _denied_at: dict[int, datetime] = field(default_factory=dict)

    def allows(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.user_ids

    def should_reply_to_denied(self, user_id: int | None, now: datetime) -> bool:
        """One short refusal per unknown user per cooldown; the id goes to the server log only."""

        if user_id is None:
            return False
        last = self._denied_at.get(user_id)
        log.warning("telegram access denied for user_id=%s", user_id)
        if last is not None and now - last < timedelta(seconds=self.deny_reply_cooldown_sec):
            return False
        self._denied_at[user_id] = now
        if len(self._denied_at) > 1000:
            oldest = sorted(self._denied_at, key=self._denied_at.get)[:500]  # type: ignore[arg-type]
            for key in oldest:
                del self._denied_at[key]
        return True
