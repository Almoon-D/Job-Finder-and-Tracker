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
from .scheduling import due_groups
from .sources.http import Http
from .state import State, iso, now_utc
from .storage.datarepo import commit_and_push

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
    due = due_groups(config, state, now, only=groups, force=force)
    if skip_polling:
        due = [d for d in due if not config.groups[d.name].interval_minutes]
    group_names = list(config.groups)
    if not due:
        log.info("nothing due")
        return 0
    log.info(f"groups due: {len(due)} ({', '.join('#' + str(group_names.index(d.name) + 1) for d in due)})")

    exit_code = 0
    run_report: dict = {"started_at": iso(now), "dry_run": dry_run, "bootstrap": bootstrap, "groups": {}}
    async with Http(config.http) as http:
        runner = Runner(config, state, http, now, bootstrap=bootstrap)
        for d in due:
            group = config.groups[d.name]
            idx = group_names.index(d.name) + 1
            if group.kind == "summary":
                log.info(f"group #{idx}: summary groups arrive in a later version; skipped")
                if not dry_run:
                    state.mark_group_run(d.name, now, d.slot)
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
            )
            if n.empty and not group.notify_empty and not n.problems:
                delivered = True
            else:
                attempted, succeeded = await send_all(config, http, n)
                delivered = attempted == 0 or succeeded > 0
                if attempted and not succeeded:
                    exit_code = 1
            if delivered:
                for job in outcome.jobs:
                    state.mark_notified(job, d.name, now)
                    for dup in job.extra.get("duplicates", []):
                        state.mark_notified(dup, d.name, now)
                state.mark_group_run(d.name, now, d.slot)
            else:
                log.warn(f"group #{idx}: no channel delivered; will retry on the next run")

    if dry_run:
        return exit_code
    only_polling = all(config.groups[d.name].interval_minutes for d in due)
    if only_polling and not state.material and not bootstrap:
        log.info("polling run without material changes: state not committed")
        return exit_code
    _persist(config, state, data_dir, now, run_report, push)
    return exit_code


def _persist(config: Config, state: State, data_dir: Path, now: datetime, report: dict, push: bool) -> None:
    from .feeds import write_feeds

    def write() -> None:
        state.save()
        write_feeds(config, state, data_dir, now, {g: v.get("last_run") for g, v in state.runs["groups"].items()})
        runs = Path(data_dir) / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "last_run.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", "utf-8")
        err = runs / "config_error.txt"
        if err.exists():
            err.unlink()

    def rewrite() -> None:
        state.merge_with_disk()
        write()

    write()
    commit_and_push(Path(data_dir), f"jobfinder: run {iso(now)}", push=push, rewrite=rewrite)


async def test_notify(config_path: str | None, data_dir: Path) -> int:
    config = load_or_report(config_path, data_dir, push=False)
    async with Http(config.http) as http:
        n = Notification("test", "test", [], config.language, config.tz, now_utc(), test=True)
        attempted, succeeded = await send_all(config, http, n)
    log.info(f"test notification: {succeeded}/{attempted} channels OK")
    if attempted == 0:
        log.warn("no notification channel is enabled in the config")
    return 0 if attempted and succeeded == attempted else 1


async def test_ai(config_path: str | None, data_dir: Path) -> int:
    """Check every AI provider that has a key. Prints provider names (from the example config) and status only."""
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
