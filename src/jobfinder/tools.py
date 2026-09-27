"""Developer tools: test a single source, check whether a browser is needed."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from . import log, sources  # noqa: F401  (registers adapters)
from .config.loader import load_config
from .config.schema import Config, Source
from .matching.location import LocationMatcher
from .sources.base import FetchContext, build_adapter
from .sources.http import Http
from .state import State, now_utc


def _find_source(config: Config | None, ref: str, type_: str | None) -> tuple[Config, Source]:
    if config is not None:
        for s in config.sources:
            if ref in (s.name, s.key):
                return config, s
    if not ref.startswith("http"):
        raise SystemExit(f"source {ref!r} not found in the config")
    base = config or Config(groups={"test": {"times": ["09:00"]}})
    group = next(iter(base.groups))
    src = Source(name="test", group=group, url=ref, type=type_)
    return base, src


async def test_source(args: argparse.Namespace, data_dir: Path) -> int:
    try:
        config = load_config(args.config, data_dir)
    except Exception:
        config = None
    config, source = _find_source(config, args.source, args.type)
    ci = log.in_ci()
    if ci:
        log.warn("running in CI: only counts are printed (logs are public)")
    matcher = LocationMatcher([] if args.no_location_filter else config.locations, config.coverage)
    state = State(Path(tempfile.gettempdir()) / "jobfinder-test-source")  # never saved
    async with Http(config.http) as http:
        ctx = FetchContext(http, config, source, state, now_utc(), matcher, 3.0)
        adapter = build_adapter(ctx)
        jobs = await adapter.fetch()
        kept = [j for j in jobs if matcher.empty or not j.locations or matcher.matches(j.locations, j.title)[0]]
        print(f"adapter: {adapter.type_name} | fetched: {len(jobs)} | in configured locations: {len(kept)}")
        if ci:
            return 0
        if args.details:
            for j in kept[: min(5, args.limit)]:
                await adapter.enrich(j)
        tz = config.tz
        for j in sorted(kept, key=lambda j: j.posted_at or now_utc(), reverse=True)[: args.limit]:
            posted = j.posted_at.astimezone(tz).strftime("%Y-%m-%d %H:%M") if j.posted_at else "?"
            print(f"- {j.title} | {j.company} | {j.location_text} | {posted} ({j.posted_precision})")
            print(f"  {j.url}")
            if args.details and j.description:
                print(f"  {j.description[:300]}…")
    return 0


def needs_browser(config_path: str | None, data_dir: Path, groups: list[str] | None = None) -> int:
    config = load_config(config_path, data_dir)
    selected = [s for g in groups for s in config.sources_for_group(g)] if groups else config.sources
    needed = any(s.enabled and s.params().get("fetch") == "browser" for s in selected)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"browser={'true' if needed else 'false'}\n")
    print("true" if needed else "false")
    return 0
