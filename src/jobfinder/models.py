"""Core data structures."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

DatePrecision = Literal["datetime", "date", "relative", "unknown"]


def normalize_text(value: str | None) -> str:
    """Lowercase, strip accents and collapse whitespace/punctuation."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower()
    value = re.sub(r"[^\w]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


@dataclass
class Job:
    source_key: str
    native_id: str
    title: str
    url: str
    company: str
    locations: list[str] = field(default_factory=list)
    posted_at: datetime | None = None
    posted_precision: DatePrecision = "unknown"
    description: str = ""
    source_type: str = ""
    kind: Literal["job", "page_change"] = "job"
    extra: dict[str, Any] = field(default_factory=dict)

    # Filled in by the pipeline
    first_seen: datetime | None = None
    score: int | None = None
    family: str | None = None
    reason: str | None = None
    experience: str | None = None
    already_alerted: bool = False
    also_on: list[str] = field(default_factory=list)
    coverage_match: bool = False

    @property
    def key(self) -> str:
        return f"{self.source_key}:{self.native_id}"

    @property
    def fingerprint(self) -> str:
        """Cross-source identity: same company + title + first city."""
        city = normalize_text(self.locations[0].split(",")[0]) if self.locations else ""
        base = "|".join([normalize_text(self.company), normalize_text(self.title), city])
        return hashlib.sha1(base.encode()).hexdigest()[:16]

    @property
    def location_text(self) -> str:
        return " · ".join(dict.fromkeys(loc for loc in self.locations if loc))


@dataclass
class SourceResult:
    source_key: str
    source_type: str
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Verdict:
    score: int
    family: str | None = None
    front_office: bool | None = None
    experience: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "family": self.family,
            "front_office": self.front_office,
            "experience": self.experience,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Verdict:
        return cls(
            score=int(d.get("score", 0)),
            family=d.get("family"),
            front_office=d.get("front_office"),
            experience=d.get("experience"),
            reason=d.get("reason"),
        )
