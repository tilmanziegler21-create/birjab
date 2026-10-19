"""JSON for the dashboard. Decimals are strings; the LLM key never appears here."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.paper.runtime import PaperRuntime, to_jsonable

router = APIRouter(prefix="/api")


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: str = ""


def _runtime(request: Request) -> PaperRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="runtime not configured")
    return runtime


@router.get("/state")
def state(request: Request) -> dict[str, Any]:
    return _runtime(request).snapshot()


@router.get("/markets")
def markets(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    snapshot = runtime.snapshot()
    return {"feed": snapshot["feed"], "news_regimes": snapshot["news"]["regimes"], "paused_reasons": snapshot["paused_reasons"]}


@router.get("/news")
def news(request: Request) -> dict[str, Any]:
    return to_jsonable(_runtime(request).news_view())


@router.get("/lots")
def lots(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    return to_jsonable({name: runtime.arm_report(name)["lots"] for name in runtime.arms})


@router.get("/trades")
def trades(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    return to_jsonable({name: runtime.arm_report(name)["fills"] for name in runtime.arms})


@router.get("/working")
def working(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    return to_jsonable({name: runtime.arm_report(name)["working"] for name in runtime.arms})


@router.get("/ab")
def ab(request: Request) -> dict[str, Any]:
    return to_jsonable(_runtime(request).ab_summary())


@router.get("/decisions")
def decisions(request: Request) -> list[dict[str, Any]]:
    return to_jsonable(_runtime(request).decision_log[-500:])


@router.get("/equity")
def equity(request: Request, limit: int = 500) -> dict[str, Any]:
    """Per-arm equity time series from the database. Empty when the runtime is not persisted."""

    if limit < 1 or limit > 20000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 20000")
    return to_jsonable(_runtime(request).equity_history(limit))


@router.post("/controls/pause")
def pause(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    runtime.pause()
    return to_jsonable({"controls": runtime.snapshot()["controls"]})


@router.post("/controls/halt")
def halt(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    runtime.halt()
    return to_jsonable({"controls": runtime.snapshot()["controls"]})


@router.post("/controls/resume")
def resume(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    runtime.resume()
    return to_jsonable({"controls": runtime.snapshot()["controls"]})


@router.post("/controls/liquidate")
def liquidate(request: Request, body: Confirmation) -> dict[str, Any]:
    runtime = _runtime(request)
    accepted = runtime.liquidate(body.confirmation)
    if not accepted:
        raise HTTPException(status_code=400, detail="confirmation string required: CONFIRM_LIQUIDATE")
    return to_jsonable({"accepted": True, "controls": runtime.snapshot()["controls"]})


@router.post("/tick")
def tick(request: Request) -> dict[str, Any]:
    """Advance one paper step by hand. Refused while the background loop owns the clock."""

    runtime = _runtime(request)
    if getattr(request.app.state, "loop_running", False):
        raise HTTPException(status_code=409, detail="background loop is running")
    report = runtime.tick(datetime.now(timezone.utc))
    return to_jsonable(report)
