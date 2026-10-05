"""Run one notification group: fetch sources, filter, score and deduplicate."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .. import log
from ..config.schema import Config, Group, Source
from ..models import Job, SourceResult, Verdict, normalize_text
from ..sources.base import Adapter, FetchContext, SkipSource, build_adapter
from ..sources.http import Http
from ..state import State
from .llm import LLMMatcher, criteria_hash
from .location import LocationMatcher
from .rules import excluded, experience_out_of_range, extract_experience, family_by_keywords

MAX_ENRICH_PER_GROUP = 60
MAX_BLOCKED_LISTED = 25  # scored matches skipped by a source's role_families that are listed in last_run.json


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
    also_elsewhere: int = 0  # matches already notified in another group (not repeated)
    # Jobs the AI scored well but a source's `role_families` refused (private: written to last_run.json only)
    blocked: list[dict] = field(default_factory=list)
    # Per source, counted once per run: {key: {"ok": bool, "jobs": n, "seconds": s, "matches": n}}
    source_stats: dict[str, dict] = field(default_factory=dict)

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
        self._recorded: set[str] = set()

    # ------------------------------------------------------------ sources
    def source_label(self, source: Source) -> str:
        """Public-safe label: index + adapter type, never the name."""
        return f"source #{self._index.get(source.key, 0)}"

    async def fetch_source(self, source: Source, max_age: float) -> tuple[SourceResult, Adapter | None]:
        cache_key = (source.key, max_age)
        if cache_key in self._cache:
            return self._cache[cache_key]
        ctx = FetchContext(self.http, self.config, source, self.state, self.now, self.locations, max_age, self.llm)
        started = time.monotonic()
        adapter: Adapter | None = None
        try:
            adapter = build_adapter(ctx)
            jobs = await asyncio.wait_for(adapter.fetch(), timeout=self.config.http.source_timeout_seconds)
            result = SourceResult(source.key, adapter.type_name, jobs, None, time.monotonic() - started)
        except SkipSource as exc:
            kind = adapter.type_name if adapter else (source.type or "?")
            result = SourceResult(source.key, kind, [], None, time.monotonic() - started, skipped=str(exc))
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
        job.extra["target"] = self.locations.target_label(job.locations)  # locations may come from enrich()
        return ok

    # ----------------------------------------------------------------- run
    async def run_group(self, name: str, group: Group) -> GroupOutcome:
        out = GroupOutcome(name, group)
        sources = self.config.sources_for_group(name)
        results = await asyncio.gather(*(self.fetch_source(s, group.max_age_days) for s in sources))

        candidates: list[tuple[Job, Source, Adapter]] = []
        for source, (result, adapter) in zip(sources, results, strict=True):
            label = self.source_label(source)
            if result.skipped:  # expected (e.g. optional credentials not set): neither ok nor a failure
                log.info(f"  {label} ({result.source_type}): skipped ({result.skipped})")
                continue
            out.sources_total += 1
            if source.key not in self._recorded:  # a source shared by two groups counts once per run
                self._recorded.add(source.key)
                self.state.record_source_result(source.key, result.ok, len(result.jobs), result.error, self.now)
                out.source_stats[source.key] = {"ok": result.ok, "jobs": len(result.jobs),
                                                "seconds": round(result.duration, 2), "matches": 0}
            if not result.ok:
                log.info(f"  {label} ({result.source_type}): error after {result.duration:.1f}s")
                log.detail(f"{source.name}: {result.error}")
                continue
            out.sources_ok += 1
            out.fetched += len(result.jobs)
            log.info(f"  {label} ({result.source_type}): {len(result.jobs)} jobs in {result.duration:.1f}s")
            first_run = not self.state.is_bootstrapped(source.key)
            for shared in result.jobs:
                # Fetch results are cached across groups: work on a per-group copy.
                job = dataclasses.replace(shared, extra={k: v for k, v in shared.extra.items() if k != "duplicates"},
                                          also_on=[])
                job.extra["target"] = self.locations.target_label(job.locations)
                self.state.observe(job, self.now, job.extra["target"])
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
        for job in out.jobs:
            stats = out.source_stats.setdefault(job.source_key, {"matches": 0})
            stats["matches"] = stats.get("matches", 0) + 1
        out.jobs.sort(key=lambda j: (-(j.score or 0), -(j.posted_at or j.first_seen or self.now).timestamp()))
        return out

    async def _score(self, items: list[tuple[Job, Source, Adapter]], out: GroupOutcome) -> list[tuple[Job, Source]]:
        ai_available = self.llm.available
        kept: list[tuple[Job, Source]] = []
        pending: list[tuple[Job, Source]] = []
        for job, source, _ in items:
            use_ai = ai_available and source.use_ai
            default_kw = self.config.filters.require_keyword_match
            if default_kw is None:
                default_kw = not use_ai
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
            require_kw = default_kw if source.require_keyword_match is None else source.require_keyword_match
            if require_kw and not fam:
                out.reject("no_keyword")
                continue
            if not use_ai:
                kept.append((job, source))
                continue
            cached = self.state.cached_verdict(job, self.criteria)
            if cached:
                self._apply(job, cached)
                if self._accept(job, source, out):
                    kept.append((job, source))
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
                    else:  # not cached, so the next run retries it while it is still fresh
                        out.reject("ai_unavailable")
                    continue
                self.state.store_verdict(job, self.criteria, v)
                self._apply(job, v)
                if self._accept(job, source, out):
                    kept.append((job, source))
        return kept

    @staticmethod
    def _apply(job: Job, v: Verdict) -> None:
        job.score = v.score
        job.family = v.family or job.family
        job.reason = v.reason
        job.experience = v.experience or job.experience

    def _accept(self, job: Job, source: Source, out: GroupOutcome) -> bool:
        """Apply the AI verdict; a refusal is counted under its real reason.

        ``ai_score``: below ``min_score``. ``source_family``: scored well, but the AI put it in a role family
        that this source's ``role_families`` does not allow (e.g. an IR job at a source limited to banking).
        The second is a config choice that silently hides good jobs, so those are listed for the owner.
        """
        if (job.score or 0) < self.config.llm.min_score:
            out.reject("ai_score")
            return False
        if source.role_families and job.family and job.family not in source.role_families:
            out.reject("source_family")
            if len(out.blocked) < MAX_BLOCKED_LISTED:
                out.blocked.append({"source": source.name, "company": job.company, "title": job.title,
                                    "family": job.family, "score": job.score, "url": job.url})
            return False
        return True

    def _dedupe(self, items: list[tuple[Job, Source]], group: str, out: GroupOutcome) -> list[Job]:
        """Drop duplicates: within this run (fuzzy fingerprint or same URL), already sent in this group,
        and, in scheduled groups, already sent in another group from another source."""
        polling = bool(self.config.groups[group].interval_minutes) if group in self.config.groups else False
        seen_here: dict[str, Job] = {}
        result: list[Job] = []
        # Stronger candidates first, so the copy that is kept is the best-scored one.
        for job, source in sorted(items, key=lambda it: -(it[0].score or 0)):
            target = job.extra.get("target") or ""
            tokens = [f"fp:{job.fingerprint}|{target}"] + ([f"url:{job.canonical_url}"] if job.canonical_url else [])
            first = next((seen_here[t] for t in tokens if t in seen_here), None)
            if first is not None:
                if source.name not in first.also_on and normalize_text(source.name) != normalize_text(first.company):
                    first.also_on.append(source.name)
                dups = first.extra.setdefault("duplicates", [])
                if all(d is not job for d in dups):
                    dups.append(job)
                out.reject("duplicate")
                continue
            previous = self.state.notified_duplicates(job, job.extra.get("target"))
            if any(group in groups for groups in previous.values()):
                out.reject("duplicate_previous")
                continue
            if previous and not polling:
                for key in previous:
                    self.state.add_also_on(key, source.name)
                out.also_elsewhere += 1
                out.reject("duplicate_other_group")
                continue
            if group != "favorites" and "favorites" in self.state.notified_groups(job):
                job.already_alerted = True
            for t in tokens:
                seen_here[t] = job
            result.append(job)
        return result
