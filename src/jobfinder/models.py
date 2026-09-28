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


# Words that never help to tell two offers apart: legal forms, connectors, gender markers.
_COMPANY_NOISE = {"sa", "sl", "slu", "sau", "ag", "ltd", "limited", "llc", "llp", "lp", "inc", "plc", "gmbh", "sarl",
                  "sas", "spa", "bv", "nv", "co", "cie", "company", "group", "grupo", "groupe", "holding",
                  "holdings", "corp", "corporation", "the", "and", "y", "et", "und", "de", "cv", "sab", "sapi"}
_TITLE_NOISE = {"and", "y", "e", "et", "und", "of", "de", "del", "des", "du", "la", "el", "los", "las", "le", "les",
                "the", "a", "an", "en", "in", "for", "para", "pour", "fur", "with", "con", "avec", "at", "to", "da",
                "all", "genders", "gender", "hybrid", "hibrido", "remote", "remoto", "teletrabajo", "office",
                "m", "f", "w", "h", "d", "x", "mwd", "fmd", "hf", "fh", "hm", "mh"}
# Place names (countries in several languages, configured cities): filled by LocationMatcher.
PLACE_WORDS: set[str] = set()


def fuzzy_company(value: str | None) -> str:
    """'J.P. Morgan Chase & Co.' -> 'jpmorganchase'; 'Banco Ejemplo, S.A.' -> 'bancoejemplo'."""
    tokens = normalize_text(value).split()
    merged: list[str] = []
    for tok in tokens:  # "j p morgan" -> "jp morgan"
        if len(tok) == 1 and merged and len(merged[-1]) <= 3 and merged[-1].isalpha() and tok.isalpha():
            merged[-1] += tok
        else:
            merged.append(tok)
    return "".join(t for t in merged if t not in _COMPANY_NOISE)


def fuzzy_title(value: str | None) -> str:
    """Order-free title key without gender markers, numbers, connectors or place names."""
    words = normalize_text(value).split()
    phrase = " ".join(words)
    for place in sorted((p for p in PLACE_WORDS if " " in p), key=len, reverse=True):
        phrase = re.sub(rf"(?<!\w){re.escape(place)}(?!\w)", " ", phrase)
    keep = {w for w in phrase.split() if w not in _TITLE_NOISE and w not in PLACE_WORDS and not w.isdigit()}
    return " ".join(sorted(keep))


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
        """Cross-source identity: fuzzy company + fuzzy title (see fuzzy_company / fuzzy_title).

        Without a known company (e.g. some e-mail alerts) the job only matches itself.
        """
        company = fuzzy_company(self.company)
        base = f"{company}|{fuzzy_title(self.title)}" if company else f"{self.key}|{normalize_text(self.title)}"
        return hashlib.sha1(base.encode()).hexdigest()[:16]

    @property
    def canonical_url(self) -> str:
        return canonical_url(self.url)

    @property
    def location_text(self) -> str:
        return " · ".join(dict.fromkeys(loc for loc in self.locations if loc))


# Query parameters that only track clicks (never identify the job).
TRACKING_PARAM = re.compile(r"^(utm_|trk|tracking|ref(id)?$|mc_|_hs|gclid|fbclid|trackingid|lipi|midtoken|midsig|"
                            r"eid$|otptoken|from$|alid|jrtk|tk$|sid$|source$|src$|rank$|page$|sessionid|userid|"
                            r"uuid|origin$|lc$)", re.I)


def canonical_url(url: str) -> str:
    """URL without scheme, 'www.', tracking parameters, fragment or trailing slash (duplicate detection)."""
    from urllib.parse import parse_qsl, urlencode, urlsplit

    parts = urlsplit(url or "")
    host = parts.netloc.lower().removeprefix("www.")
    if not host:
        return ""
    query = sorted((k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAM.match(k))
    return f"{host}{parts.path.rstrip('/')}" + (f"?{urlencode(query)}" if query else "")


@dataclass
class SourceResult:
    source_key: str
    source_type: str
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None
    duration: float = 0.0
    skipped: str | None = None  # reason, when the source did not run on purpose (safe to log)

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
