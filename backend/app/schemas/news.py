"""Structured news analysis. Article text is untrusted data, never instructions."""

from __future__ import annotations

from enum import Enum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventType(str, Enum):
    NETWORK_INCIDENT = "network_incident"
    REGULATORY = "regulatory"
    EXCHANGE_OPS = "exchange_ops"
    DELISTING = "delisting"
    DEPEG = "depeg"
    MARKET_COLOR = "market_color"
    UNKNOWN = "unknown"


class SourceQuality(str, Enum):
    OFFICIAL = "official"
    LICENSED = "licensed"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class Sentiment(str, Enum):
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    POSITIVE = "positive"
    UNKNOWN = "unknown"


class Novelty(str, Enum):
    NEW = "new"
    UPDATE = "update"
    DUPLICATE = "duplicate"
    UNKNOWN = "unknown"


class ImpactWindow(str, Enum):
    H0_4 = "0_4h"
    H4_24 = "4_24h"
    D1_7 = "1_7d"
    UNKNOWN = "unknown"


class RiskRecommendation(str, Enum):
    ALLOW = "allow"
    REDUCE_SIZE = "reduce_size"
    PAUSE_NEW_BUYS = "pause_new_buys"
    HALT_ALL = "halt_all"


class NewsAnalysis(BaseModel):
    """LLM output schema. There is no order, price target, or probability field."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)
    symbols: list[str] = Field(default_factory=list, max_length=32)
    event_type: EventType
    source_quality: SourceQuality
    severity: Severity
    sentiment: Sentiment
    novelty: Novelty
    expected_impact_window: ImpactWindow
    hypotheses: list[str] = Field(default_factory=list, max_length=8)
    unknowns: list[str] = Field(default_factory=list, max_length=8)
    risk_recommendation: RiskRecommendation
    rationale: str = Field(max_length=2000)
    source_urls: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            symbol = item.strip().upper()
            if symbol == "MARKET":
                cleaned.append(symbol)
                continue
            if not symbol.isascii() or not symbol.isalnum() or not 2 <= len(symbol) <= 10:
                raise ValueError(f"unsupported symbol token: {item}")
            cleaned.append(symbol)
        return cleaned

    @field_validator("source_urls")
    @classmethod
    def _urls(cls, value: list[str]) -> list[str]:
        checked: list[str] = []
        for item in value:
            parsed = urlparse(item)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("source URLs must be http(s)")
            checked.append(item)
        return checked

    @field_validator("hypotheses", "unknowns")
    @classmethod
    def _text_list(cls, value: list[str]) -> list[str]:
        return [item[:500] for item in value]


class InterpretedNews(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    analysis: NewsAnalysis | None = None
    safe_recommendation: RiskRecommendation
    reason: str


def interpret_model_output(payload: object) -> InterpretedNews:
    """Validate model output. Invalid, injected, or non-object payloads pause buys."""

    if not isinstance(payload, dict):
        return InterpretedNews(
            ok=False,
            analysis=None,
            safe_recommendation=RiskRecommendation.PAUSE_NEW_BUYS,
            reason="analysis_invalid",
        )
    forbidden = {"place_order", "probability", "order", "api_key", "withdraw"}
    extra = forbidden.intersection(payload.keys())
    try:
        analysis = NewsAnalysis.model_validate(payload)
    except Exception:
        return InterpretedNews(
            ok=False,
            analysis=None,
            safe_recommendation=RiskRecommendation.PAUSE_NEW_BUYS,
            reason="analysis_invalid" if not extra else "analysis_rejected_fields",
        )
    return InterpretedNews(
        ok=True,
        analysis=analysis,
        safe_recommendation=analysis.risk_recommendation,
        reason="parsed",
    )


def assert_payload_is_data(payload: dict[str, Any]) -> None:
    """News fields are stored as text. This function intentionally performs no I/O."""

    for value in payload.values():
        if callable(value):
            raise TypeError("news payload must not contain callables")
