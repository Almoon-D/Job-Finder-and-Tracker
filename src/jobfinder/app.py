"""High-level commands used by the CLI."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import log
from .config.loader import ConfigError, load_config
from .config.schema import Config, Group, Source
from .matching.pipeline import GroupOutcome, Runner
from .notify.base import Notification
from .notify.dispatch import send_all
from .scheduling import DueGroup, due_groups
from .sources.http import Http
from .state import State, iso, now_utc
from .stats import RunStats
from .storage.datarepo import commit_and_push
from .tracker.store import Tracker, open_tracker

DEFAULT_LABELS = {
    "es": {"company_sites": "Webs corporativas", "favorites": "Empresas favoritas", "boards": "Portales de empleo",
           "recruiters": "Cazatalentos", "weekly_summary": "Resumen semanal"},
    "en": {"company_sites": "Company career sites", "favorites": "Favourite companies", "boards": "Job boards",
           "recruiters": "Recruiters", "weekly_summary": "Weekly summary"},
}


def group_label(config: Config, name: str, group: Group) -> str:
    return group.label or DEFAULT_LABELS.get(config.language, {}).get(name) or name


def load_or_report(config_path: str | None, data_dir: Path, push: bool) -> Config:
    try:
        return load_config(config_path, data_dir)
    except ConfigError as exc:
        log.warn(exc.public)
        if data_dir.exists():
            # Full error (may contain private values) goes to the private data repo only.
            err = data_dir / "runs" / "config_error.txt"
            err.parent.mkdir(parents=True, exist_ok=True)
            err.write_text(exc.detail + "\n", encoding="utf-8")
            commit_and_push(data_dir, "jobfinder: config error", push=push)
            log.info("full error written to runs/config_error.txt in the data repository")
        raise


def _problems(config: Config, state: State, sources: list[Source]) -> list[str]:
    names = {s.key: s.name for s in sources}
    out = []
    lang = config.language
    for key, problem in state.health_problems(list(names), config.notify.health_alert_after_failures):
        if problem == "zero":
            from .i18n import t

            problem = t(lang, "zero")
        out.append(f"{names[key]} ({problem})")
    return out


def _print_outcome(o: GroupOutcome, config: Config) -> None:
    """Local (non-CI) console output of matches, for dry runs."""
    tz = config.tz
    if o.rejected:
        print("  discarded: " + ", ".join(f"{k}={v}" for k, v in sorted(o.rejected.items())))
    for j in o.jobs:
        posted = j.posted_at.astimezone(tz).strftime("%d/%m %H:%M") if j.posted_at else "?"
        score = f"{j.score:>3}" if j.score is not None else "  -"
        print(f"  [{score}] {j.company} — {j.title} | {j.location_text} | {posted} | {j.url}")
        if j.reason:
            print(f"        {j.reason}")


async def run(
    config_path: str | None,
    data_dir: Path,
    groups: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    push: bool = True,
    bootstrap: bool = False,
    now: datetime | None = None,
    skip_polling: bool = False,
) -> int:
    config = load_or_report(config_path, data_dir, push and not dry_run)
    state = State(data_dir)
    now = now or now_utc()
    if bootstrap:
        force = True
    # Tracker buttons and commands are read at the start of every normal run (see tracker/sync.py).
    tracker = None if dry_run or bootstrap else open_tracker(config, data_dir)
    due = due_groups(config, state, now, only=groups, force=force)
    if skip_polling:
        due = [d for d in due if not config.groups[d.name].interval_minutes]
    due.sort(key=lambda d: config.groups[d.name].kind == "summary")  # summaries last: they count this run
    group_names = list(config.groups)

    exit_code = 0
    run_report: dict = {"started_at": iso(now), "dry_run": dry_run, "bootstrap": bootstrap, "groups": {}}
    stats = RunStats()
    reports: dict[str, str] = {}
    async with Http(config.http) as http:
        if tracker is not None:
            from .tracker.sync import sync

            await sync(config, http, tracker, state, now)
        if not due:
            log.info("nothing due")
            if tracker is not None and tracker.changed:
                _persist(config, state, data_dir, now, None, push, tracker=tracker,
                         message=f"jobfinder: tracker sync {iso(now)}")
            return 0
        log.info(f"groups due: {len(due)} ({', '.join('#' + str(group_names.index(d.name) + 1) for d in due)})")
        runner = Runner(config, state, http, now, bootstrap=bootstrap)
        for d in due:
            group = config.groups[d.name]
            idx = group_names.index(d.name) + 1
            if group.kind == "summary":
                if bootstrap:
                    continue
                code = await _run_summary(config, state, tracker, http, data_dir, now, d, idx, dry_run, stats,
                                          reports)
                run_report["groups"][d.name] = {"kind": "summary", "delivered": code == 0,
                                                "reports": list(reports)}
                exit_code = max(exit_code, code)
                continue
            log.info(f"group #{idx}: {len(config.sources_for_group(d.name))} sources")
            outcome = await runner.run_group(d.name, group)
            log.info(
                f"group #{idx}: fetched {outcome.fetched}, candidates {outcome.candidates}, "
                f"ai-scored {outcome.scored_by_ai}, matches {len(outcome.jobs)}, "
                f"sources ok {outcome.sources_ok}/{outcome.sources_total}"
            )
            run_report["groups"][d.name] = {
                "fetched": outcome.fetched,
                "candidates": outcome.candidates,
                "matches": len(outcome.jobs),
                "ai_scored": outcome.scored_by_ai,
                "rejected": outcome.rejected,
                "also_elsewhere": outcome.also_elsewhere,
                "sources_ok": outcome.sources_ok,
                "sources_total": outcome.sources_total,
                "errors": {s.name: state.source_info(s.key).get("last_error")
                           for s in config.sources_for_group(d.name)
                           if state.source_info(s.key).get("fail_streak")},
            }
            if bootstrap:
                state.mark_group_run(d.name, now, d.slot)
                continue
            stats.add_group(d.name, run_report["groups"][d.name], outcome.source_stats)
            if dry_run:
                if log.in_ci():
                    log.info("dry run in CI: matches are not printed (public logs)")
                else:
                    _print_outcome(outcome, config)
                continue

            n = Notification(
                group=d.name,
                label=group_label(config, d.name, group),
                jobs=outcome.jobs,
                lang=config.language,
                tz=config.tz,
                now=now,
                priority=group.priority,
                format=group.format,
                sources_ok=outcome.sources_ok,
                sources_total=outcome.sources_total,
                problems=[] if group.interval_minutes else _problems(config, state, config.sources_for_group(d.name)),
                max_items=group.max_items,
                family_labels={f.name: f.label or f.name.replace("_", " ").capitalize() for f in config.role_families},
                place_order=[tg.display for tg in runner.locations.targets],
                also_elsewhere=outcome.also_elsewhere,
                buttons=tracker is not None and group.format == "per_job",
                tracker_status=tracker.statuses() if tracker is not None else {},
            )
            if n.empty and not group.notify_empty and not n.problems:
                delivered = True
            else:
                attempted, succeeded = await send_all(config, http, n)
                delivered = attempted == 0 or succeeded > 0
                if attempted and not succeeded:
                    exit_code = 1
            if tracker is not None:
                day = tracker.today(now).isoformat()
                for jid, key, chat, message_id in n.sent:
                    tracker.remember(jid, key, chat, message_id, day)
            if delivered:
                for job in outcome.jobs:
                    state.mark_notified(job, d.name, now)
                    for dup in job.extra.get("duplicates", []):
                        state.mark_notified(dup, d.name, now, dup_of=job.key)
                state.mark_group_run(d.name, now, d.slot)
            else:
                log.warn(f"group #{idx}: no channel delivered; will retry on the next run")

    if dry_run:
        return exit_code
    only_polling = all(config.groups[d.name].interval_minutes for d in due)
    tracker_changed = tracker is not None and tracker.changed
    if only_polling and not state.material and not bootstrap and not tracker_changed:
        log.info("polling run without material changes: state not committed")
        return exit_code
    _persist(config, state, data_dir, now, run_report, push, tracker=tracker,
             stats=None if bootstrap else stats, reports=reports)
    return exit_code


async def _run_summary(config: Config, state: State, tracker: Tracker | None, http: Http, data_dir: Path,
                       now: datetime, due: DueGroup, idx: int, dry_run: bool, stats: RunStats,
                       reports: dict[str, str]) -> int:
    """Build and send a weekly summary; its Markdown file is written when the run is persisted."""
    from .weekly import build_weekly

    group = config.groups[due.name]
    tracker = tracker or open_tracker(config, data_dir)  # dry runs: read only, never saved
    labels = {name: group_label(config, name, g) for name, g in config.groups.items()}
    weekly = build_weekly(config, state, tracker, data_dir, due.slot or now, group_label(config, due.name, group),
                          labels, stats)
    log.info(f"group #{idx}: weekly summary with {weekly.total} new jobs")
    if dry_run:
        if not log.in_ci():
            print(weekly.markdown)
        return 0
    n = Notification(due.name, weekly.report.title, [], config.language, config.tz, now, report=weekly.report)
    attempted, succeeded = await send_all(config, http, n)
    if attempted and not succeeded:
        log.warn(f"group #{idx}: no channel delivered; will retry on the next run")
        return 1
    reports[weekly.path] = weekly.markdown
    state.mark_group_run(due.name, now, due.slot)
    return 0


def _persist(config: Config, state: State, data_dir: Path, now: datetime, report: dict | None, push: bool,
             tracker: Tracker | None = None, stats: RunStats | None = None, reports: dict[str, str] | None = None,
             message: str | None = None) -> None:
    """Write state, tracker, stats, reports and feeds, then commit and push the data repo.

    ``report`` (runs/last_run.json) is None for runs that only synced the tracker.
    """
    from .feeds import write_feeds

    data_dir = Path(data_dir)

    def write() -> None:
        state.save()
        if tracker is not None:
            tracker.save(now)
        runs = data_dir / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        if stats is not None and not stats.empty:
            stats.merge_into(data_dir, now.astimezone(config.tz).date())  # reads what is on disk: see stats.py
        for rel, text in (reports or {}).items():
            path = data_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        write_feeds(config, state, data_dir, now, {g: v.get("last_run") for g, v in state.runs["groups"].items()},
                    tracker)
        if report is not None:
            (runs / "last_run.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", "utf-8")
        err = runs / "config_error.txt"
        if err.exists():
            err.unlink()

    def rewrite() -> None:
        state.merge_with_disk()
        if tracker is not None:
            tracker.merge_with_disk(now)
        write()

    write()
    commit_and_push(data_dir, message or f"jobfinder: run {iso(now)}", push=push, rewrite=rewrite)


async def tracker_sync(config_path: str | None, data_dir: Path, push: bool = True, now: datetime | None = None) -> int:
    """Read tracker buttons and commands from Telegram and save them (cron-job.org, every 1–3 h)."""
    from .tracker.sync import sync

    config = load_or_report(config_path, data_dir, push)
    tracker = open_tracker(config, data_dir)
    if tracker is None:
        log.info("tracker: disabled (needs tracker.enabled and notify.telegram.enabled)")
        return 0
    state = State(data_dir)
    now = now or now_utc()
    async with Http(config.http) as http:
        result = await sync(config, http, tracker, state, now)
    if tracker.changed:
        _persist(config, state, data_dir, now, None, push, tracker=tracker,
                 message=f"jobfinder: tracker sync {iso(now)}")
    else:
        log.info("tracker: nothing to save")
    return 1 if result.failed else 0


async def test_notify(config_path: str | None, data_dir: Path) -> int:
    config = load_or_report(config_path, data_dir, push=False)
    async with Http(config.http) as http:
        n = Notification("test", "test", [], config.language, config.tz, now_utc(), test=True,
                         buttons=open_tracker(config, data_dir) is not None)
        attempted, succeeded = await send_all(config, http, n)
    log.info(f"test notification: {succeeded}/{attempted} channels OK")
    if attempted == 0:
        log.warn("no notification channel is enabled in the config")
    return 0 if attempted and succeeded == attempted else 1


async def test_ai(config_path: str | None, data_dir: Path) -> int:
    """Check every AI provider that has a key. Logs only each provider's number, `name` and status."""
    from .matching.llm import LLMMatcher

    config = load_or_report(config_path, data_dir, push=False)
    if not config.llm.enabled:
        log.info("ai: disabled in the config")
        return 0
    async with Http(config.http) as http:
        matcher = LLMMatcher(config, http)
        without_key = len(config.llm.providers) - len(matcher.providers)
        if not matcher.providers:
            log.warn("ai: no provider has its API key set")
            return 1
        results = await matcher.check()
    index = {id(p): i for i, p in enumerate(config.llm.providers, 1)}
    for provider, status, seconds in results:
        log.info(f"ai: provider #{index[id(provider)]} ({provider.name}): {status} in {seconds:.1f}s")
    if without_key:
        log.info(f"ai: {without_key} provider(s) without API key skipped")
    return 0 if any(status == "OK" for _, status, _ in results) else 1
