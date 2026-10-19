"""One story, many links. Normalization is local string work."""

from __future__ import annotations

import hashlib
import re


def normalize_url(url: str) -> str:
    raw = url.strip().split("#", 1)[0]
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        scheme = scheme.lower()
    else:
        scheme, rest = "", raw
    if "/" in rest:
        host, path = rest.split("/", 1)
        path = "/" + path
    else:
        host, path = rest, ""
    if "?" in path:
        path, query = path.split("?", 1)
    else:
        query = ""
    path = path.rstrip("/").lower()
    kept: list[str] = []
    for part in query.split("&"):
        if not part:
            continue
        key = part.split("=", 1)[0].lower()
        if key.startswith("utm_"):
            continue
        kept.append(part)
    base = f"{scheme}://{host.lower()}{path}" if scheme else f"{host.lower()}{path}"
    if kept:
        return base + "?" + "&".join(kept)
    return base


def text_fingerprint(headline: str, summary: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", f"{headline} {summary}".lower())
    return " ".join(tokens)


def same_story(left: str, right: str) -> bool:
    """Token overlap of 0.8 or more is the same story. Compared with integers."""

    a = set(left.split())
    b = set(right.split())
    if not a or not b:
        return False
    overlap = len(a & b)
    union = len(a | b)
    return overlap * 5 >= union * 4


def canonical_event_id(fingerprint: str) -> str:
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
    return f"evt-{digest}"
