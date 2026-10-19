"""Deterministic local analyst. It validates fixtures; it does not invent news."""

from __future__ import annotations

from app.schemas.news import InterpretedNews, interpret_model_output


class MockAIAnalyst:
    def analyze(self, payload: object) -> InterpretedNews:
        return interpret_model_output(payload)
