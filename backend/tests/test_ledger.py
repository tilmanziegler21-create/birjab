from __future__ import annotations

from decimal import Decimal
from threading import Thread

import pytest

from app.accounting.fees import buy_economics, meets_net_target, round_trip_net_pct, sell_economics
from app.accounting.ledger import IdempotencyConflict, InsufficientLotQuantity, LedgerHalted, OutOfFunds
from app.accounting.pnl import liquid_equity
from app.accounting.pnl import OpenLotMark
from app.money import MoneyError, money
from tests.conftest import open_ledger


def test_float_money_is_rejected() -> None:
    with pytest.raises(MoneyError):
        money(0.1)


def test_round_trip_after_fees_and_slippage() -> None:
    before_slippage = round_trip_net_pct(
        step_pct=Decimal("0.30"),
        entry_fee_pct=Decimal("0.10"),
        exit_fee_pct=Decimal("0.10"),
        slippage_pct=Decimal("0"),
    )
    after_slippage = round_trip_net_pct(
        step_pct=Decimal("0.30"),
        entry_fee_pct=Decimal("0.10"),
        exit_fee_pct=Decimal("0.10"),
        slippage_pct=Decimal("0.05"),
    )
    assert before_slippage == Decimal("0.10")
    assert after_slippage == Decimal("0.05")
    assert meets_net_target(expected_net_pct=after_slippage, net_target_pct=Decimal("0.10")) is False
    negative = round_trip_net_pct(
        step_pct=Decimal("0.30"),
        entry_fee_pct=Decimal("0.10"),
        exit_fee_pct=Decimal("0.10"),
        slippage_pct=Decimal("0.15"),
    )
    assert negative == Decimal("-0.05")


def test_quote_fee_buy_and_sell_match_known_decimals() -> None:
    ledger = open_ledger()
    ledger.reserve("r1", "BTC/USDC", Decimal("100.1"))
    lot = ledger.apply_buy_fill(
        fill_id="f1",
        reservation_id="r1",
        lot_id="lot-1",
        symbol="BTC/USDC",
        price=Decimal("100"),
        quantity=Decimal("1"),
        fee_amount=Decimal("0.1"),
        fee_currency="USDC",
    )
    assert lot.sellable_base_qty == Decimal("1")
    assert lot.quote_cost_remaining == Decimal("100.1")
    assert ledger.free == Decimal("4899.9")
    assert ledger.reserved == Decimal("0")

    bid = Decimal("100")
    marked = ledger.equity({"BTC/USDC": bid}, Decimal("0.10"))
    assert marked.gross_position_value == Decimal("100")
    assert marked.estimated_exit_fees == Decimal("0.1")
    assert marked.net_position_value == Decimal("99.9")
    assert marked.equity == marked.cash + marked.net_position_value
    assert marked.equity == Decimal("4999.8")

    realized = ledger.apply_sell_fill(
        fill_id="s1",
        lot_id="lot-1",
        price=Decimal("100.3"),
        quantity=Decimal("1"),
        fee_amount=Decimal("0.1003"),
        fee_currency="USDC",
    )
    assert realized == Decimal("0.0997")
    assert ledger.free == Decimal("5000.0997")
    assert ledger.lots["lot-1"].status == "closed"
    assert ledger.fees_by_currency["USDC"] == Decimal("0.2003")
    flat = ledger.equity({}, Decimal("0.10"))
    assert flat.equity == Decimal("5000.0997")
    assert flat.realized_pnl == Decimal("0.0997")
    assert flat.unrealized_pnl == Decimal("0")


def test_base_fee_reduces_sellable_quantity() -> None:
    economics = buy_economics(
        price=Decimal("100"),
        quantity=Decimal("1"),
        fee_amount=Decimal("0.001"),
        fee_currency="BTC",
        base_asset="BTC",
        quote_asset="USDC",
    )
    assert economics.quote_debit == Decimal("100")
    assert economics.sellable_base == Decimal("0.999")

    ledger = open_ledger()
    ledger.reserve("r1", "BTC/USDC", Decimal("100"))
    lot = ledger.apply_buy_fill(
        fill_id="f1",
        reservation_id="r1",
        lot_id="lot-1",
        symbol="BTC/USDC",
        price=Decimal("100"),
        quantity=Decimal("1"),
        fee_amount=Decimal("0.001"),
        fee_currency="BTC",
    )
    assert lot.sellable_base_qty == Decimal("0.999")
    with pytest.raises(InsufficientLotQuantity):
        ledger.apply_sell_fill(
            fill_id="s1",
            lot_id="lot-1",
            price=Decimal("100"),
            quantity=Decimal("1"),
            fee_amount=Decimal("0"),
            fee_currency="USDC",
        )
    assert ledger.lots["lot-1"].remaining_base_qty == Decimal("0.999")
    assert ledger.fees_by_currency["BTC"] == Decimal("0.001")


def test_partial_fill_then_release_returns_only_unused_reserve() -> None:
    ledger = open_ledger()
    ledger.reserve("r1", "ETH/USDC", Decimal("100"))
    ledger.apply_buy_fill(
        fill_id="f1",
        reservation_id="r1",
        lot_id="lot-1",
        symbol="ETH/USDC",
        price=Decimal("100"),
        quantity=Decimal("0.4"),
        fee_amount=Decimal("0"),
        fee_currency="USDC",
    )
    assert ledger.free == Decimal("4900")
    assert ledger.reserved == Decimal("60")
    ledger.apply_buy_fill(
        fill_id="f1",
        reservation_id="r1",
        lot_id="lot-1",
        symbol="ETH/USDC",
        price=Decimal("100"),
        quantity=Decimal("0.4"),
        fee_amount=Decimal("0"),
        fee_currency="USDC",
    )
    assert len(ledger.lots) == 1
    assert ledger.reserved == Decimal("60")
    ledger.release("r1")
    ledger.release("r1")
    assert ledger.free == Decimal("4960")
    assert ledger.reserved == Decimal("0")
    assert ledger.free + ledger.reserved == ledger.starting + ledger.quote_in - ledger.quote_out


def test_duplicate_fill_with_different_terms_does_not_mutate() -> None:
    ledger = open_ledger()
    ledger.reserve("r1", "BTC/USDC", Decimal("100"))
    ledger.apply_buy_fill(
        fill_id="f1",
        reservation_id="r1",
        lot_id="lot-1",
        symbol="BTC/USDC",
        price=Decimal("100"),
        quantity=Decimal("1"),
        fee_amount=Decimal("0"),
        fee_currency="USDC",
    )
    before = (ledger.free, ledger.reserved, len(ledger.lots))
    with pytest.raises(IdempotencyConflict):
        ledger.apply_buy_fill(
            fill_id="f1",
            reservation_id="r1",
            lot_id="lot-1",
            symbol="BTC/USDC",
            price=Decimal("99"),
            quantity=Decimal("1"),
            fee_amount=Decimal("0"),
            fee_currency="USDC",
        )
    assert (ledger.free, ledger.reserved, len(ledger.lots)) == before


def test_out_of_funds_and_idempotent_reserve() -> None:
    ledger = open_ledger(balance="50")
    ledger.reserve("r1", "BTC/USDC", Decimal("50"))
    with pytest.raises(OutOfFunds):
        ledger.reserve("r2", "ETH/USDC", Decimal("1"))
    assert ledger.free == Decimal("0")
    assert ledger.reserved == Decimal("50")
    ledger.reserve("r1", "BTC/USDC", Decimal("50"))
    assert ledger.reserved == Decimal("50")
    assert len([event for event in ledger.events if event.event_type == "RESERVE"]) == 1


def test_quote_currency_is_not_hardcoded_to_usdt() -> None:
    eur = open_ledger(quote="EUR", balance="1000")
    eur.reserve("r1", "BTC/EUR", Decimal("10"))
    assert eur.quote_currency is not None
    assert eur.quote_currency.value == "EUR"
    usdt = open_ledger(quote="USDT", balance="1000")
    usdt.reserve("r1", "ETH/USDT", Decimal("10"))
    assert usdt.quote_currency is not None
    assert usdt.quote_currency.value == "USDT"
    mismatch = open_ledger(quote="USDC")
    with pytest.raises(Exception):
        mismatch.reserve("r1", "BTC/USDT", Decimal("10"))
    assert mismatch.free == Decimal("5000")


def test_blocked_ledger_refuses_buys_and_sells() -> None:
    ledger = open_ledger()
    ledger.reserve("r1", "BTC/USDC", Decimal("100"))
    ledger.apply_buy_fill(
        fill_id="f1",
        reservation_id="r1",
        lot_id="lot-1",
        symbol="BTC/USDC",
        price=Decimal("100"),
        quantity=Decimal("1"),
        fee_amount=Decimal("0"),
        fee_currency="USDC",
    )
    ledger.mark_blocked("balance_snapshot_mismatch")
    with pytest.raises(LedgerHalted):
        ledger.reserve("r2", "ETH/USDC", Decimal("10"))
    with pytest.raises(LedgerHalted):
        ledger.apply_sell_fill(
            fill_id="s1",
            lot_id="lot-1",
            price=Decimal("100"),
            quantity=Decimal("1"),
            fee_amount=Decimal("0"),
            fee_currency="USDC",
        )
    assert ledger.lots["lot-1"].remaining_base_qty == Decimal("1")


def test_concurrent_reserves_cannot_spend_the_same_cash() -> None:
    ledger = open_ledger(balance="100")
    results: list[str] = []

    def worker(index: int) -> None:
        try:
            ledger.reserve(f"r{index}", "BTC/USDC", Decimal("80"))
            results.append("ok")
        except OutOfFunds:
            results.append("short")

    threads = [Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["ok", "short"]
    assert ledger.free == Decimal("20")
    assert ledger.reserved == Decimal("80")


def test_sell_economics_fee_in_quote() -> None:
    result = sell_economics(
        price=Decimal("100.3"),
        quantity=Decimal("1"),
        fee_amount=Decimal("0.1003"),
        fee_currency="USDC",
        base_asset="BTC",
        quote_asset="USDC",
    )
    assert result.quote_credit == Decimal("100.1997")


def test_two_lots_are_not_pooled() -> None:
    ledger = open_ledger()
    ledger.reserve("r1", "BTC/USDC", Decimal("50"))
    ledger.reserve("r2", "BTC/USDC", Decimal("50"))
    ledger.apply_buy_fill(
        fill_id="f1",
        reservation_id="r1",
        lot_id="lot-1",
        symbol="BTC/USDC",
        price=Decimal("100"),
        quantity=Decimal("0.5"),
        fee_amount=Decimal("0"),
        fee_currency="USDC",
    )
    ledger.apply_buy_fill(
        fill_id="f2",
        reservation_id="r2",
        lot_id="lot-2",
        symbol="BTC/USDC",
        price=Decimal("90"),
        quantity=Decimal("0.5"),
        fee_amount=Decimal("0"),
        fee_currency="USDC",
    )
    with pytest.raises(InsufficientLotQuantity):
        ledger.apply_sell_fill(
            fill_id="s1",
            lot_id="lot-1",
            price=Decimal("100"),
            quantity=Decimal("0.6"),
            fee_amount=Decimal("0"),
            fee_currency="USDC",
        )
    assert ledger.lots["lot-2"].remaining_base_qty == Decimal("0.5")


def test_equity_helper_uses_bid_not_a_stablecoin_mark() -> None:
    breakdown = liquid_equity(
        free_quote=Decimal("10"),
        reserved_quote=Decimal("5"),
        lots=[
            OpenLotMark(
                symbol="SOL/EUR",
                remaining_base_qty=Decimal("2"),
                quote_cost_remaining=Decimal("20"),
                bid=Decimal("9"),
            )
        ],
        exit_fee_pct=Decimal("0"),
        realized_pnl=Decimal("0"),
        fees_by_currency={},
    )
    assert breakdown.equity == Decimal("33")
    assert breakdown.cash == Decimal("15")
