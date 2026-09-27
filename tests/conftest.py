from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jobfinder import sources  # noqa: F401  (registers adapters)
from jobfinder.config.schema import Config
from jobfinder.models import Job
from jobfinder.sources.base import Adapter, register

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 27, 7, 30, tzinfo=UTC)  # 09:30 in Berlin (CEST)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@register
class FakeAdapter(Adapter):
    """Returns the jobs listed in the source params (used by pipeline tests)."""

    type_name = "fake"
    calls = 0

    async def fetch(self) -> list[Job]:
        FakeAdapter.calls += 1
        if self.params.get("fail"):
            raise RuntimeError("boom")
        out = []
        for j in self.params.get("jobs", []):
            posted = j.get("posted")
            out.append(self.job(j["id"], j["title"], f"https://jobs.example.test/{j['id']}",
                                locations=j.get("locations", []),
                                posted_at=datetime.fromisoformat(posted) if posted else None,
                                posted_precision="datetime" if posted else "unknown",
                                description=j.get("description", ""),
                                company=j.get("company")))
        return out


def make_config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "timezone": "Europe/Berlin",
        "language": "es",
        "role_families": [
            {"name": "product", "include_any": ["product manager", "data analyst"]},
            {"name": "ux", "include_any": ["ux research"]},
        ],
        "filters": {"exclude_title_any": ["compliance", "intern*"]},
        "locations": [{"country": "DE"}, {"city": "Lisbon", "country": "PT"}],
        "coverage": {"text_any": ["DACH"]},
        "groups": {
            "favorites": {"interval_minutes": 10, "format": "per_job", "priority": "high", "notify_empty": False},
            "company_sites": {"times": ["09:00", "20:30"]},
        },
        "sources": [],
        "notify": {"telegram": {"enabled": False}},
    }
    base.update(overrides)
    return Config.model_validate(base)


@pytest.fixture
def now() -> datetime:
    return NOW
