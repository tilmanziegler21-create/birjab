from __future__ import annotations

import ast
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect

from app.accounting.fees import meets_net_target, round_trip_net_pct
from app.adapters.exchange import LiveAdapter, LiveTradingDisabledError
from app.adapters.paper import PaperAdapter, PaperExecutionNotImplemented
from app.config import load_settings
from app.db.models import Base
from app.execution.orders import InvalidOrderTransition, OrderState, transition
from app.money import QuoteCurrency
from app.schemas.orders import OrderIntent

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def test_live_adapter_cannot_send_or_keep_credentials() -> None:
    adapter = LiveAdapter(api_key="local-test-key", api_secret="local-test-secret", passphrase="local-pass")
    blob = repr(adapter) + repr(vars(adapter))
    assert "local-test-key" not in blob
    assert "local-test-secret" not in blob
    assert adapter.enabled is False
    with pytest.raises(LiveTradingDisabledError):
        adapter.place_order()
    with pytest.raises(LiveTradingDisabledError):
        adapter.cancel_order("abc")
    with pytest.raises(LiveTradingDisabledError):
        adapter.withdraw()
    paper = PaperAdapter()
    intent = OrderIntent(
        client_order_id="buy-1",
        symbol="BTC/USDC",
        side="buy",
        purpose="entry",
        quote_value=Decimal("50"),
        reason="paper",
        strategy_id="baseline_grid",
    )
    with pytest.raises(PaperExecutionNotImplemented):
        paper.place_order(intent)


def test_adapter_modules_do_not_import_network_clients() -> None:
    banned = {"socket", "httpx", "urllib", "requests", "ccxt", "websockets", "aiohttp", "openai"}
    for path in (BACKEND / "app" / "adapters").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported.isdisjoint(banned)


def test_paper_config_loads_and_live_flag_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings(ROOT / "configs" / "paper.example.yaml")
    assert settings.mode == "paper"
    assert settings.allow_live_trading is False
    assert settings.quote_currency == QuoteCurrency.USDC
    assert settings.starting_virtual_balance == Decimal("5000")
    assert settings.watchlist == ["BTC", "ETH", "SOL", "XRP", "BNB", "LINK", "ADA", "AVAX"]
    expected = round_trip_net_pct(
        step_pct=settings.grid_step_pct,
        entry_fee_pct=settings.assumed_maker_fee_pct,
        exit_fee_pct=settings.assumed_taker_fee_pct,
        slippage_pct=settings.assumed_slippage_pct,
    )
    assert expected == Decimal("0.05")
    assert meets_net_target(expected_net_pct=expected, net_target_pct=settings.net_target_pct) is False

    disabled = load_settings(ROOT / "configs" / "live.DISABLED.example.yaml")
    assert disabled.allow_live_trading is False
    assert disabled.mode == "paper"

    monkeypatch.setenv("ATS_QUOTE_CURRENCY", "EUR")
    overridden = load_settings(ROOT / "configs" / "paper.example.yaml")
    assert overridden.quote_currency == QuoteCurrency.EUR
    assert overridden.starting_virtual_balance == Decimal("5000")

    live = tmp_path / "live.yaml"
    live.write_text("mode: paper\nallow_live_trading: true\nquote_currency: USDC\n", encoding="utf-8")
    monkeypatch.delenv("ATS_QUOTE_CURRENCY", raising=False)
    with pytest.raises(ValidationError):
        load_settings(live)


def test_order_unknown_state_is_not_resubmitted() -> None:
    assert transition(OrderState.PREPARED, OrderState.SUBMITTED) == OrderState.SUBMITTED
    assert transition(OrderState.UNKNOWN, OrderState.FILLED) == OrderState.FILLED
    with pytest.raises(InvalidOrderTransition):
        transition(OrderState.UNKNOWN, OrderState.SUBMITTED)
    with pytest.raises(InvalidOrderTransition):
        transition(OrderState.FILLED, OrderState.OPEN)


def test_health_has_no_order_route_and_no_secrets() -> None:
    from app.main import create_app

    app = create_app()
    paths = app.openapi()["paths"]
    assert "/health" in paths
    assert all("order" not in path for path in paths)
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "paper"
    assert body["live_trading"] is False
    assert body["live_adapter"] == "disabled"
    assert "last_tick_at" in body
    assert body["quote_currency"] in {"USDC", "USDT", "EUR"}
    text = response.text.lower()
    assert "api_key" not in text
    assert "secret" not in text


def test_source_tree_has_no_live_secrets() -> None:
    import re

    banned = (
        re.compile(r"sk-[A-Za-z0-9]{8,}"),
        re.compile("BEGIN " + "PRIVATE KEY"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
    )
    skip = {".git", ".venv", "venv", "__pycache__", "node_modules", "pgdata", "tests"}
    for path in ROOT.rglob("*"):
        if any(part in skip for part in path.parts) or not path.is_file():
            continue
        if path.suffix not in {".py", ".yml", ".yaml", ".toml", ".md", ".example", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in banned:
            assert pattern.search(text) is None, f"{pattern.pattern} in {path}"


def test_alembic_upgrade_creates_model_tables(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/mig.db"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND)
    subprocess.check_call(
        [sys.executable, "-m", "alembic", "-x", f"url={url}", "upgrade", "head"],
        cwd=BACKEND,
        env=env,
    )
    engine = create_engine(url)
    names = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) <= names
    assert "alembic_version" in names
