"""Map a validated analysis to a regime. The model text is not executed."""

from __future__ import annotations

from app.schemas.news import EventType, NewsAnalysis, RiskRecommendation, Severity, SourceQuality
from app.schemas.risk import RiskRegime


def regime_from_analysis(
    analysis: NewsAnalysis, *, pause_if_unverified_high_impact: bool
) -> RiskRegime:
    if analysis.risk_recommendation == RiskRecommendation.HALT_ALL:
        return RiskRegime.HALT_ALL
    if analysis.event_type == EventType.DEPEG:
        return RiskRegime.DEPEG
    if analysis.event_type == EventType.EXCHANGE_OPS and analysis.severity == Severity.HIGH:
        return RiskRegime.EXCHANGE_ISSUE
    if analysis.event_type == EventType.DELISTING:
        return RiskRegime.EXCHANGE_ISSUE
    unverified = analysis.source_quality in {SourceQuality.UNVERIFIED, SourceQuality.UNKNOWN}
    if (
        pause_if_unverified_high_impact
        and unverified
        and analysis.severity == Severity.HIGH
    ):
        return RiskRegime.PAUSE_BUYS
    if analysis.risk_recommendation == RiskRecommendation.PAUSE_NEW_BUYS:
        return RiskRegime.PAUSE_BUYS
    if analysis.risk_recommendation == RiskRecommendation.REDUCE_SIZE:
        return RiskRegime.CAUTION
    if analysis.severity == Severity.HIGH:
        return RiskRegime.HIGH_RISK
    return RiskRegime.NORMAL
