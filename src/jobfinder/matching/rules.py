"""Cheap, deterministic filters applied before (or instead of) the AI matcher."""

from __future__ import annotations

import re
from functools import lru_cache

from ..config.schema import Config, RoleFamily, Source
from ..models import Job, normalize_text


@lru_cache(maxsize=4096)
def _pattern(keyword: str) -> re.Pattern[str]:
    kw = keyword.strip()
    prefix = kw.endswith("*")
    norm = normalize_text(kw.rstrip("*"))
    body = re.escape(norm).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){body}" + ("" if prefix else r"(?!\w)"))


def find_keyword(text: str, keywords: list[str], normalized: bool = False) -> str | None:
    """First keyword found in text as a whole word/phrase ('advis*' = prefix match)."""
    norm = text if normalized else normalize_text(text)
    for kw in keywords:
        if kw.strip() and _pattern(kw).search(norm):
            return kw
    return None


def allowed_families(config: Config, source: Source) -> list[RoleFamily]:
    if source.role_families is None:
        return list(config.role_families)
    return [f for f in config.role_families if f.name in source.role_families]


def excluded(job: Job, config: Config, source: Source) -> str | None:
    """Return the reason if a hard exclusion applies."""
    title = normalize_text(job.title)
    kw = find_keyword(title, config.filters.exclude_title_any + source.exclude_title_any, normalized=True)
    if kw:
        return f"title:{kw}"
    if config.filters.exclude_text_any:
        kw = find_keyword(f"{job.title} {job.description}", config.filters.exclude_text_any)
        if kw:
            return f"text:{kw}"
    return None


def family_by_keywords(job: Job, config: Config, source: Source) -> str | None:
    title = normalize_text(job.title)
    if source.include_title_any and find_keyword(title, source.include_title_any, normalized=True):
        return source.role_families[0] if source.role_families else "match"
    for fam in allowed_families(config, source):
        if find_keyword(title, fam.include_any, normalized=True):
            return fam.name
    return None


_RANGE = re.compile(
    r"(\d{1,2})\s*(?:-|–|to|a|à|y|and)\s*(\d{1,2})\s*\+?\s*(?:years|year|yrs|años|anos|ans|jahre)", re.I
)
_MIN = re.compile(
    r"(?:at least|minimum(?: of)?|min\.?|más de|mas de|al menos|mínimo|minimo|au moins|plus de)?\s*"
    r"(\d{1,2})\s*\+?\s*(?:years|year|yrs|años|anos|ans|jahre)",
    re.I,
)


def extract_experience(text: str) -> tuple[float | None, float | None, str | None]:
    """('3-5 years' -> (3, 5, '3-5')), ('5+ years' -> (5, None, '5+'))."""
    if not text:
        return None, None, None
    m = _RANGE.search(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if 0 < lo <= hi <= 30:
            return lo, hi, f"{lo}-{hi}"
    m = _MIN.search(text)
    if m:
        lo = int(m.group(1))
        if 0 < lo <= 30:
            return lo, None, f"{lo}+"
    return None, None, None


def experience_out_of_range(job: Job, config: Config) -> bool:
    exp = config.experience
    lo, hi, _ = extract_experience(job.description)
    if lo is None:
        return False
    if exp.max_years is not None and lo > exp.max_years + 1:
        return True
    return exp.min_years is not None and hi is not None and hi < exp.min_years - 1
