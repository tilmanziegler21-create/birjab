"""Live adapter. Disabled. Credentials passed to the constructor are discarded."""

from __future__ import annotations


class LiveTradingDisabledError(RuntimeError):
    pass


class LiveAdapter:
    enabled = False
    name = "live-disabled"

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
    ) -> None:
        self.credentials_stored = False
        del api_key, api_secret, passphrase

    def place_order(self, *args: object, **kwargs: object) -> None:
        raise LiveTradingDisabledError("Live trading is disabled in this build. No order was sent.")

    def cancel_order(self, *args: object, **kwargs: object) -> None:
        raise LiveTradingDisabledError("Live trading is disabled in this build. No cancel was sent.")

    def withdraw(self, *args: object, **kwargs: object) -> None:
        raise LiveTradingDisabledError("Withdrawals are disabled. No transfer was sent.")
