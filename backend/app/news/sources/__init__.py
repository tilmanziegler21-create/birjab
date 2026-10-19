"""Illustrative news fixtures. No credentials are read here."""

from app.news.sources.mock import MockNewsSource, RawItem

__all__ = ["MockNewsSource", "RawItem"]