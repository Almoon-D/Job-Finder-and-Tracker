"""Run one notification group: fetch sources, filter, score and deduplicate."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .. import log
from ..config.schema import Config, Group, Source
from ..models import Job, SourceResult, Verdict, normalize_text
from ..sources.base import Adapter, FetchContext, build_adapter
from ..sources.http import Http
from ..state import State
from .llm import LLMMatcher, criteria_hash
from .location import LocationMatcher
from .rules import excluded, experience_out_of_range, extract_experience, family_by_keywords

MAX_ENRICH_PER_GROUP = 60


@dataclass
class GroupOutcome:
    group: str
    config: Group
    jobs: list[Job] = field(default_factory=list)
    sources_total: int = 0
    sources_ok: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)
    fetched: int = 0
    candidates: int = 0
    scored_by_ai: int = 0
    rejected: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


class Runner:
    def __init__(self, config: Config, state: State, http: Http, now: datetime, bootstrap: bool = False):
        self.config = config
        self.state = state
        self.http = http
        self.now = now
        self.bootstrap = bootstrap
        self.locations = LocationMatcher(config.locations, config.coverage)
        self.llm = LLMMatcher(config, http)
        self.criteria = criteria_hash(config)
        self._cache: dict[tuple[str, float], tuple[SourceResult, Adapter | None]] = {}
        self._index = {s.key: i + 1 for i, s in enumerate(config.sources)}

    # ------------------------------------------------------------ sources
    def source_label(self, source: Source) -> str:
        """Public-safe label: index + adapter type, never the name."""
        return f"source #{self._index.get(source.key, 0)}"

    async def fetch_source(self, source: Source, max_age: float) -> tuple[SourceResult, Adapter | None]:
        cache_key = (source.key, max_age)
        if cache_key in self._cache:
            return self._cache[cache_key]
        ctx = FetchContext(self.http, self.config, source, self.state, self.now, self.locations, max_age)
        started = time.monotonic()
        adapter: Adapter | None = None
        try:
            adapter = build_adapter(ctx)
            jobs = await asyncio.wait_for(adapter.fetch(), timeout=self.config.http.source_timeout_seconds)
            result = SourceResult(source.key, adapter.type_name, jobs, None, time.monotonic() - started)
        except Exception as exc:  # one broken source must never break the run
            kind = adapter.type_name if adapter else (source.type or "?")
            msg = f"{type(exc).__name__}: {exc}"[:300]
            result = SourceResult(source.key, kind, [], msg, time.monotonic() - started)
        self._cache[cache_key] = (result, adapter)
        return result, adapter

    async def _enrich(self, pairs: list[tuple[Job, Adapter]]) -> None:
        sem = asyncio.Semaphore(6)

        async def one(job: Job, adapter: Adapter) -> None:
            async with sem:
                try:
                    await asyncio.wait_for(adapter.enrich(job), timeout=60)
                except Exception as exc:  # details are optional
                    log.detail(f"enrich failed for {job.url}: {exc!r}")

        await asyncio.gather(*(one(j, a) for j, a in pairs))

    # ------------------------------------------------------------- filters
    def _fresh(self, job: Job, max_age_days: float) -> bool:
        ref = job.posted_at or job.first_seen or self.now
        return self.now - ref <= timedelta(days=max_age_days)

    def _location_ok(self, job: Job, source: Source) -> bool:
        if source.skip_location_filter or job.kind == "page_change" or not job.locations:
            return True
        ok, via_coverage = self.locations.matches(job.locations, f"{job.title} {job.description}")
        job.coverage_match = via_coverage
        return ok

    # ----------------------------------------------------------------- run
    async def run_group(self, name: str, group: Group) -> GroupOutcome:
        out = GroupOutcome(name, group)
        sources = self.config.sources_for_group(name)
        out.sources_total = len(sources)
        results = await asyncio.gather(*(self.fetch_source(s, group.max_age_days) for s in sources))

        candidates: list[tuple[Job, Source, Adapter]] = []
        for source, (result, adapter) in zip(sources, results, strict=True):
            self.state.record_source_result(source.key, result.ok, len(result.jobs), result.error, self.now)
            label = self.source_label(source)
            if not result.ok:
                log.info(f"  {label} ({result.source_type}): error after {result.duration:.1f}s")
                log.detail(f"{source.name}: {result.error}")
                continue
            out.sources_ok += 1
            out.fetched += len(result.jobs)
            log.info(f"  {label} ({result.source_type}): {len(result.jobs)} jobs in {result.duration:.1f}s")
            first_run = not self.state.is_bootstrapped(source.key)
            for job in result.jobs:
                self.state.observe(job, self.now)
                if self.bootstrap:
                    self.state.mark_baseline(job)
                    continue
                if self.state.notified_in(job, name):
                    out.reject("already_notified")
                    continue
                if job.posted_at is None and job.kind == "job":
                    # Undated jobs present when a source is first added are the baseline, never "new".
                    if first_run:
                        self.state.mark_baseline(job)
                    if self.state.is_baseline(job):
                        out.reject("baseline")
                        continue
                if not self._fresh(job, group.max_age_days):
                    out.reject("too_old")
                    continue
                candidates.append((job, source, adapter))  # type: ignore[arg-type]
            self.state.set_bootstrapped(source.key)

        if self.bootstrap:
            return out

        # Multi-location postings: get the real locations before filtering by place.
        multi = [(j, a) for j, s, a in candidates if j.extra.get("multi_location") and s.fetch_details]
        await self._enrich(multi[:MAX_ENRICH_PER_GROUP])

        stage2: list[tuple[Job, Source, Adapter]] = []
        for job, source, adapter in candidates:
            if not self._location_ok(job, source):
                out.reject("location")
                continue
            if job.kind == "job":
                reason = excluded(job, self.config, source)
                if reason:
                    out.reject("excluded")
                    continue
            stage2.append((job, source, adapter))
        out.candidates = len(stage2)

        # Full descriptions only for the survivors (and only those not already scored).
        to_enrich = [
            (j, a) for j, s, a in stage2
            if s.fetch_details and not j.description and j.kind == "job"
            and self.state.cached_verdict(j, self.criteria) is None and not j.extra.get("multi_location")
        ]
        await self._enrich(to_enrich[:MAX_ENRICH_PER_GROUP])

        matched = await self._score(stage2, out)
        out.jobs = self._dedupe(matched, name, out)
        out.jobs.sort(key=lambda j: (-(j.score or 0), -(j.posted_at or j.first_seen or self.now).timestamp()))
        return out

    async def _score(self, items: list[tuple[Job, Source, Adapter]], out: GroupOutcome) -> list[tuple[Job, Source]]:
        use_ai = self.llm.available
        require_kw = self.config.filters.require_keyword_match
        if require_kw is None:
            require_kw = not use_ai

        kept: list[tuple[Job, Source]] = []
        pending: list[tuple[Job, Source]] = []
        for job, source, _ in items:
            if job.kind != "job":
                kept.append((job, source))
                continue
            if job.description and excluded(job, self.config, source):
                out.reject("excluded")
                continue
            if self.config.experience.hard and experience_out_of_range(job, self.config):
                out.reject("experience")
                continue
            job.experience = extract_experience(job.description)[2]
            fam = family_by_keywords(job, self.config, source)
            job.family = fam
            if require_kw and not fam:
                out.reject("no_keyword")
                continue
            if not use_ai:
                kept.append((job, source))
                continue
            cached = self.state.cached_verdict(job, self.criteria)
            if cached:
                self._apply(job, cached)
                if self._accept(job, source):
                    kept.append((job, source))
                else:
                    out.reject("ai_score")
                continue
            pending.append((job, source))

        if pending:
            verdicts = await self.llm.score([j for j, _ in pending])
            out.scored_by_ai += len(verdicts)
            for job, source in pending:
                v = verdicts.get(job.key)
                if v is None:  # AI unavailable: fall back to keywords
                    if job.family:
                        kept.append((job, source))
                    else:
                        out.reject("no_keyword")
                    continue
                self.state.store_verdict(job, self.criteria, v)
                self._apply(job, v)
                if self._accept(job, source):
                    kept.append((job, source))
                else:
                    out.reject("ai_score")
        return kept

    @staticmethod
    def _apply(job: Job, v: Verdict) -> None:
        job.score = v.score
        job.family = v.family or job.family
        job.reason = v.reason
        job.experience = v.experience or job.experience

    def _accept(self, job: Job, source: Source) -> bool:
        if (job.score or 0) < self.config.llm.min_score:
            return False
        return not (source.role_families and job.family and job.family not in source.role_families)

    def _dedupe(self, items: list[tuple[Job, Source]], group: str, out: GroupOutcome) -> list[Job]:
        by_fp: dict[str, Job] = {}
        result: list[Job] = []
        for job, source in items:
            fp = job.fingerprint
            if fp in by_fp:
                first = by_fp[fp]
                if source.name not in first.also_on and normalize_text(source.name) != normalize_text(first.company):
                    first.also_on.append(source.name)
                first.extra.setdefault("duplicates", []).append(job)
                out.reject("duplicate")
                continue
            if self.state.fingerprint_notified(fp, group, exclude_key=job.key):
                out.reject("duplicate_previous")
                continue
            if group != "favorites" and "favorites" in self.state.notified_groups(job):
                job.already_alerted = True
            by_fp[fp] = job
            result.append(job)
        return result
