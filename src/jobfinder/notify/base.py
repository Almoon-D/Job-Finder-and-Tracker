"""Notification model and channel interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config.schema import Config
from ..i18n import t, when
from ..models import Job
from ..sources.http import Http


class ChannelError(Exception):
    pass


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

    def job_heading(self, job: Job) -> str:
        if job.kind == "page_change":
            return f"{job.company} — {t(self.lang, 'page_changed')}"
        return f"{job.company} — {job.title}"


class Channel(ABC):
    name: str

    def __init__(self, config: Config, http: Http):
        self.config = config
        self.http = http

    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    async def send(self, n: Notification) -> None: ...
