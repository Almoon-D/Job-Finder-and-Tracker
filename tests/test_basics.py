"""Dates, locations, rules and config validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobfinder.config.loader import ConfigError, _parse
from jobfinder.config.schema import Coverage, LocationSpec, Source
from jobfinder.dates import parse_date
from jobfinder.matching.location import LocationMatcher, resolve_country
from jobfinder.matching.rules import excluded, extract_experience, family_by_keywords, find_keyword
from jobfinder.models import Job

from .conftest import NOW, make_config


# ------------------------------------------------------------------ dates
@pytest.mark.parametrize(
    "text,days,precision",
    [
        ("Posted Today", 0, "relative"),
        ("Posted Yesterday", 1, "relative"),
        ("Posted 3 Days Ago", 3, "relative"),
        ("Posted 30+ Days Ago", 30, "relative"),
        ("hace 2 días", 2, "relative"),
        ("il y a 5 jours", 5, "relative"),
    ],
)
def test_relative_dates(text, days, precision):
    dt, prec = parse_date(text, NOW)
    assert prec == precision
    assert abs((NOW - dt) - timedelta(days=days)) < timedelta(minutes=1)


def test_absolute_dates():
    assert parse_date("2026-09-25", NOW) == (datetime(2026, 9, 25, tzinfo=UTC), "date")
    dt, prec = parse_date("2026-09-25T10:15:00Z", NOW)
    assert prec == "datetime" and dt.hour == 10
    dt, prec = parse_date(1790380800, NOW)
    assert prec == "datetime" and dt.year == 2026
    dt, prec = parse_date(1790380800000, NOW)  # milliseconds
    assert dt.year == 2026
    assert parse_date("25/09/2026", NOW)[0] == datetime(2026, 9, 25, tzinfo=UTC)  # day first
    assert parse_date("09/25/2026", NOW)[0] == datetime(2026, 9, 25, tzinfo=UTC)  # impossible DMY -> MDY
    assert parse_date(None, NOW) == (None, "unknown")
    assert parse_date("not a date at all zzz", NOW)[1] == "unknown"


# --------------------------------------------------------------- location
@pytest.fixture(scope="module")
def matcher():
    return LocationMatcher(
        [
            LocationSpec(country="DE"),
            LocationSpec(city="Lisbon", country="PT"),
            LocationSpec(city="Toronto", country="CA", aliases=["GTA"]),
        ],
        Coverage(text_any=["DACH", "German market"]),
    )


@pytest.mark.parametrize(
    "loc,expected",
    [
        ("Berlin, Berlin, Germany", True),
        ("10115, BERLIN, Berlin", True),  # major city without country
        ("DEU - Hamburg", True),  # ISO3 code token
        ("Remote - Deutschland", True),
        ("Lisboa, PT", True),
        ("Lisbon Office New", True),
        ("Lisbonne", True),
        ("Vienna, Austria", False),
        ("Toronto, Ontario", True),
        ("GTA", True),
        ("Vancouver, Canada", False),
        ("Toronto, Ohio, United States", False),
        ("Hamburg, New York, United States", False),
        ("London, United Kingdom", False),
        ("", False),
    ],
)
def test_location_matching(matcher, loc, expected):
    assert matcher.match_location(loc) is expected


def test_coverage(matcher):
    ok, via = matcher.matches(["London, United Kingdom"], "Account Manager covering DACH clients")
    assert ok and via
    assert matcher.matches(["London, United Kingdom"], "Account Manager Nordics") == (False, False)
    assert matcher.matches(["Berlin, Germany"], "") == (True, False)


def test_resolve_country():
    assert resolve_country("Germany") == "DE"
    assert resolve_country("deutschland") == "DE"
    assert resolve_country("PT") == "PT"
    assert resolve_country("CAN") == "CA"
    assert resolve_country("Narnia") is None


# ------------------------------------------------------------------ rules
def _job(title: str, desc: str = "") -> Job:
    return Job("s", "1", title, "https://x.test/1", "Acme", description=desc)


def test_keywords_and_exclusions():
    cfg = make_config()
    source = Source(name="Acme", group="company_sites", url="https://x.test")
    assert find_keyword("Senior Product Manager - DACH", ["product manager"]) == "product manager"
    assert find_keyword("Internship program", ["intern*"]) == "intern*"
    assert find_keyword("International sales", ["intern"]) is None  # whole words only
    assert excluded(_job("Product Compliance Officer"), cfg, source) == "title:compliance"
    assert excluded(_job("Summer Internship"), cfg, source) == "title:intern*"
    assert excluded(_job("Product Manager"), cfg, source) is None
    assert family_by_keywords(_job("Data Analyst (m/f/d)"), cfg, source) == "product"
    assert family_by_keywords(_job("Software Engineer"), cfg, source) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We require 3-5 years of experience", "3-5"),
        ("minimum 5+ years in product management", "5+"),
        ("Experiencia de 4 a 6 años en producto", "4-6"),
        ("au moins 7 ans d'expérience", "7+"),
        ("no experience mentioned", None),
    ],
)
def test_experience(text, expected):
    assert extract_experience(text)[2] == expected


# ----------------------------------------------------------------- config
def test_config_errors_are_public_safe():
    bad = """
groups: {g: {times: ["25:00"]}}
sources:
  - {name: "Secret Corp Name", group: missing, url: "https://secret.example"}
"""
    with pytest.raises(ConfigError) as err:
        _parse(bad, "test")
    assert "Secret Corp Name" not in err.value.public
    assert "secret.example" not in err.value.public
    assert "Secret Corp Name" in err.value.detail or "25:00" in err.value.detail


def test_config_numbers_become_strings():
    cfg = _parse("filters: {exclude_title_any: [2027, intern]}\ngroups: {g: {times: ['09:00']}}", "t")
    assert cfg.filters.exclude_title_any == ["2027", "intern"]


def test_config_cross_checks():
    with pytest.raises(ConfigError):
        _parse("groups: {g: {times: ['09:00']}}\nsources: [{name: a, group: g, url: 'https://x', favorite: true}]", "t")
    with pytest.raises(ConfigError):
        _parse("groups: {g: {times: ['09:00']}}\nsources: [{name: a, group: g}]", "t")


def test_example_config_is_valid():
    from pathlib import Path

    from jobfinder.config.loader import load_config
    from jobfinder.sources.detect import resolve

    cfg = load_config(Path(__file__).parent.parent / "config.example.yaml")
    for s in cfg.sources:
        resolve(s)  # every example source resolves to an adapter
