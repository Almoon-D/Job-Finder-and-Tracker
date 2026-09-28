"""Small helpers shared by adapters."""

from __future__ import annotations

import html
import os
import re
from typing import Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser


def html_to_text(value: str | None, limit: int | None = None) -> str:
    if not value:
        return ""
    if "<" not in value and "&lt;" in value:  # HTML delivered escaped (e.g. Greenhouse)
        value = html.unescape(value)
    if "<" in value:
        tree = HTMLParser(value)
        for node in tree.css("script, style"):
            node.decompose()
        text = tree.body.text(separator=" ") if tree.body else tree.text(separator=" ")
    else:
        text = html.unescape(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def skip_without(*env_names: str) -> None:
    """Skip the source (not a failure) when optional credentials are not configured."""
    from .base import SkipSource

    missing = [n for n in env_names if not os.environ.get(n, "").strip()]
    if missing:
        raise SkipSource(f"no credentials: set {', '.join(missing)}")


def slug(value: str) -> str:
    """URL slug of a search term: 'Banca Privada' -> 'banca-privada' (accents removed)."""
    from ..models import normalize_text

    return normalize_text(value).replace(" ", "-")


def absolute(base: str, href: str | None) -> str:
    return urljoin(base, href or "")


def dig(obj: Any, *path: str | int, default: Any = None) -> Any:
    for p in path:
        if isinstance(obj, dict):
            obj = obj.get(p)  # type: ignore[arg-type]
        elif isinstance(obj, list) and isinstance(p, int) and -len(obj) <= p < len(obj):
            obj = obj[p]
        else:
            return default
        if obj is None:
            return default
    return obj


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
