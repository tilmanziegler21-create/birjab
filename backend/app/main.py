"""HTTP surface. There is no order endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import Settings, load_settings
from app.paper.runtime import PaperRuntime

PHASE = 6
log = logging.getLogger("ats.main")


def _config_path() -> Path | None:
    candidates: list[Path] = []
    env_path = os.environ.get("ATS_CONFIG_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("configs/paper.example.yaml"))
    candidates.append(Path(__file__).resolve().parents[2] / "configs" / "paper.example.yaml")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _frontend_dist() -> Path | None:
    for candidate in (Path("frontend/dist"), Path(__file__).resolve().parents[2] / "frontend" / "dist"):
        if (candidate / "index.html").is_file():
            return candidate
    return None


def create_app(
    settings: Settings | None = None,
    *,
    runtime: PaperRuntime | None = None,
    build_runtime: bool = False,
) -> FastAPI:
    if settings is None:
        path = _config_path()
        settings = load_settings(path) if path is not None else Settings()
    instance_lock = None
    if runtime is None and (build_runtime or settings.run_loop):
        from app.ops.lock import InstanceLock, instance_lock_path
        from app.paper.bootstrap import build_runtime as _build

        instance_lock = InstanceLock(instance_lock_path(settings.database_url))
        if not instance_lock.acquire():
            raise RuntimeError(f"another paper process holds {instance_lock.path}; refusing a second engine")
        runtime = _build(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        telegram_task: asyncio.Task[None] | None = None
        if runtime is not None and settings.run_loop:
            app.state.loop_running = True
            task = asyncio.create_task(_loop(app, runtime, settings.market_poll_interval_sec))
        if runtime is not None and settings.telegram_inprocess:
            from app.telegram.bot import TelegramApp

            telegram_task = asyncio.create_task(_telegram(TelegramApp(runtime, settings)), name="ats-telegram")
            app.state.telegram = True
        elif not settings.telegram_enabled:
            log.warning("telegram not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS")
        try:
            yield
        finally:
            for pending in (telegram_task, task):
                if pending is not None:
                    pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pending
            if runtime is not None:
                with contextlib.suppress(Exception):
                    runtime.persist()
            app.state.loop_running = False

    app = FastAPI(title="AI Trading System", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.loop_running = False
    app.state.telegram = False
    app.state.instance_lock = instance_lock

    @app.get("/health")
    def health() -> dict[str, object]:
        last_tick = runtime.last_tick_at.isoformat() if runtime is not None and runtime.last_tick_at else None
        return {
            "service": "ai-trading-system",
            "mode": settings.mode,
            "live_trading": False,
            "live_adapter": "disabled",
            "quote_currency": settings.quote_currency.value,
            "exchange": settings.exchange,
            "phase": PHASE,
            "runtime": runtime is not None,
            "loop": bool(settings.run_loop and runtime is not None),
            "ticks": runtime.tick_count if runtime is not None else 0,
            "last_tick_at": last_tick,
            "telegram": bool(getattr(app.state, "telegram", False)),
        }

    app.include_router(router)
    dist = _frontend_dist()
    if dist is not None:
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="dashboard")
    return app


async def _telegram(telegram_app: object) -> None:
    """Polling restarts after a network failure; a dead bot never stops the paper loop."""

    while True:
        try:
            await telegram_app.run()  # type: ignore[attr-defined]
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("telegram polling failed; retrying in 30 s")
            await asyncio.sleep(30)


async def _loop(app: FastAPI, runtime: PaperRuntime, interval_sec: int) -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(None, runtime.tick)
        except Exception:  # noqa: BLE001 - keep the paper loop alive, log the failure
            log.exception("paper tick failed")
        await asyncio.sleep(interval_sec)


app = create_app(build_runtime=os.environ.get("ATS_RUNTIME") == "1")
