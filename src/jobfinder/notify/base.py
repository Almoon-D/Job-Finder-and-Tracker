"""Notification model and channel interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..config.schema import Config
from ..i18n import t, when
from ..models import Job
from ..sources.http import Http

if TYPE_CHECKING:
    from .destinations import Destinations
    from .routing import Router


class ChannelError(Exception):
    pass


@dataclass
class Report:
    """A text report (e.g. the weekly summary) sent instead of a list of jobs."""

    title: str
    sections: list[tuple[str, list[str]]]  # (heading, plain-text lines)
    footer: list[str] = field(default_factory=list)


@dataclass
class Notification:
    group: str
    label: str
    jobs: list[Job]
    lang: str
    tz: ZoneInfo
    now: datetime
    priority: str = "normal"
    format: str = "digest"
    sources_ok: int = 0
    sources_total: int = 0
    problems: list[str] = field(default_factory=list)
    max_items: int = 50
    test: bool = False
    family_labels: dict[str, str] = field(default_factory=dict)  # family name -> display name, in config order
    place_order: list[str] = field(default_factory=list)  # location display names, in config order
    also_elsewhere: int = 0  # matches not repeated because another group already sent them
    buttons: bool = False  # tracker buttons (Telegram) / reactions (Discord bot) on per-job and grouped alerts
    tracker_status: dict[str, str] = field(default_factory=dict)  # job id -> current tracker status
    sent: list[tuple[str, str, str, int]] = field(default_factory=list)  # (job id, job key, chat, message id)
    layouts: list[tuple[str, int, list[str], int]] = field(default_factory=list)  # grouped: chat, message, jids, start
    report: Report | None = None
    silent: bool = False  # delivered without a sound (the Resumen index: every offer already rings in its own chat)
    show_sources: bool = True  # the "sources: n/m" footer line (only the chat that carries the health report)

    @property
    def empty(self) -> bool:
        return not self.jobs

    @property
    def shown(self) -> list[Job]:
        return self.jobs[: self.max_items]

    @property
    def hidden_count(self) -> int:
        return max(0, len(self.jobs) - self.max_items)

    @property
    def title(self) -> str:
        if self.report is not None:
            return self.report.title
        if self.test:
            return f"🔔 {t(self.lang, 'test_title')}"
        icon = "⚡" if self.priority == "high" else "💼"
        if self.empty:
            return f"{icon} {self.label}: {t(self.lang, 'no_news')}"
        n = len(self.jobs)
        count = t(self.lang, "new_job") if n == 1 else t(self.lang, "new_jobs", n=n)
        return f"{icon} {self.label}: {count}"

    @property
    def sources_line(self) -> str:
        return t(self.lang, "sources", ok=self.sources_ok, total=self.sources_total)

    def when(self, job: Job) -> str:
        return when(job, self.tz, self.lang, self.now)

    def job_meta(self, job: Job) -> list[str]:
        """Secondary lines for a job, plain text."""
        lines = []
        loc = job.location_text
        if job.coverage_match:
            loc = f"{loc} ({t(self.lang, 'coverage')})" if loc else t(self.lang, "coverage")
        if loc:
            lines.append(f"📍 {loc}")
        lines.append(f"🕒 {self.when(job)}")
        extra = []
        if job.score is not None:
            extra.append(f"⭐ {t(self.lang, 'fit')} {job.score}/100")
        if job.experience:
            extra.append(f"🎓 {t(self.lang, 'exp')} {job.experience} {t(self.lang, 'years')}")
        if extra:
            lines.append(" · ".join(extra))
        if job.reason:
            lines.append(f"💬 {job.reason}")
        flags = []
        if job.already_alerted:
            flags.append(f"⚡ {t(self.lang, 'already')}")
        if job.also_on:
            flags.append(f"{t(self.lang, 'also_on')}: {', '.join(job.also_on)}")
        if flags:
            lines.append(" · ".join(flags))
        return lines

    def sections(self) -> list[tuple[str, list[Job]]]:
        """Shown jobs grouped by role family and place: [("Private banking · Geneva (3)", jobs)]."""
        other = t(self.lang, "other")
        families = list(self.family_labels)
        buckets: dict[tuple[str, str], list[Job]] = {}
        for job in self.shown:
            fam = job.family if job.family in self.family_labels else ""
            place = job.extra.get("target") or (t(self.lang, "coverage_place") if job.coverage_match else "")
            buckets.setdefault((fam, place), []).append(job)

        def order(key: tuple[str, str]) -> tuple[int, int, str]:
            fam, place = key
            fi = families.index(fam) if fam in families else len(families)
            pi = self.place_order.index(place) if place in self.place_order else len(self.place_order) + (not place)
            return fi, pi, place

        out = []
        for key in sorted(buckets, key=order):
            fam, place = key
            heading = " · ".join([self.family_labels.get(fam) or other] + ([place] if place else []))
            out.append((f"{heading} ({len(buckets[key])})", buckets[key]))
        return out

    def compact_meta(self, job: Job) -> str:
        """One-line secondary info for grouped digests: company · place · fit · date · flags."""
        bits = [job.company] if job.company else []
        if job.location_text:
            bits.append(f"📍 {job.location_text[:60]}")
        if job.score is not None:
            bits.append(f"⭐ {job.score}")
        ref = job.posted_at or job.first_seen or self.now
        bits.append(f"🕒 {'~' if job.posted_precision == 'relative' else ''}{ref.astimezone(self.tz):%d/%m}")
        if job.already_alerted:
            bits.append(f"⚡ {t(self.lang, 'already')}")
        if job.also_on:
            bits.append(f"{t(self.lang, 'also_on')}: {', '.join(job.also_on)}")
        return " · ".join(bits)

    def job_heading(self, job: Job) -> str:
        if job.kind == "page_change":
            return f"{job.company} — {t(self.lang, 'page_changed')}"
        return f"{job.company} — {job.title}"


class Channel(ABC):
    name: str

    def __init__(self, config: Config, http: Http, destinations: Destinations | None = None):
        self.config = config
        self.http = http
        self.destinations = destinations  # None: never routed (one chat), whatever notify.routing says

    @cached_property
    def router(self) -> Router:
        from .routing import Router  # routing.py imports this module

        return Router(self.config)

    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    async def send(self, n: Notification) -> None: ...
