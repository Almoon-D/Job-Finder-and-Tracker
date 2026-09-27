"""Command line interface: ``jobfinder <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from . import __version__, log
from .config.loader import ConfigError


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="path to config.yaml (default: <data-dir>/config.yaml or $JOBFINDER_CONFIG)")
    p.add_argument("--data-dir", default=os.environ.get("JOBFINDER_DATA_DIR", "data"),
                   help="private data repository checkout (default: ./data)")
    p.add_argument("-v", "--verbose", action="store_true", help="print private details (ignored in CI)")


def _load_secrets_json() -> None:
    """GitHub Actions passes every repository secret as JSON in JOBFINDER_SECRETS (toJSON(secrets)).

    This lets the config reference any env var name (e.g. a custom api_key_env)
    without editing the workflow. Values already in the environment win.
    """
    raw = os.environ.pop("JOBFINDER_SECRETS", "")
    if not raw:
        return
    try:
        data = json.loads(raw)
    except ValueError:
        log.warn("JOBFINDER_SECRETS is not valid JSON; ignored")
        return
    for key, value in data.items():
        if isinstance(value, str) and value and key != "github_token" and not os.environ.get(key):
            os.environ[key] = value


def _groups(value: str | None) -> list[str] | None:
    return [g.strip() for g in value.split(",") if g.strip()] if value else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobfinder", description="Job finder & tracker for GitHub Actions")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run the groups that are due and notify")
    _common(p)
    p.add_argument("--groups", help="comma-separated group names to consider")
    p.add_argument("--force", action="store_true", help="run the groups even if not due")
    p.add_argument("--dry-run", action="store_true", help="do not notify, save state or push")
    p.add_argument("--no-push", action="store_true", help="save state locally but do not push")
    p.add_argument("--skip-polling", action="store_true",
                   help="ignore interval (polling) groups; they have their own workflow")

    p = sub.add_parser("dry-run", help="alias of: run --force --dry-run")
    _common(p)
    p.add_argument("--groups")

    p = sub.add_parser("bootstrap", help="mark every current job as seen, without notifying")
    _common(p)
    p.add_argument("--groups")
    p.add_argument("--no-push", action="store_true")

    p = sub.add_parser("validate-config", help="validate the config")
    _common(p)

    p = sub.add_parser("test-notify", help="send a test message to every enabled channel")
    _common(p)

    p = sub.add_parser("test-source", help="fetch one source (by name, id or URL) and print what it finds")
    _common(p)
    p.add_argument("source", help="source name/id from the config, or a careers URL")
    p.add_argument("--type", help="adapter type when testing a URL that cannot be auto-detected")
    p.add_argument("--no-location-filter", action="store_true")
    p.add_argument("--details", action="store_true", help="also fetch descriptions for the first jobs")
    p.add_argument("--limit", type=int, default=25)

    p = sub.add_parser("detect", help="show which adapter handles a careers URL")
    p.add_argument("url")

    p = sub.add_parser("linkedin-id", help="print the LinkedIn company id of a company page (for company_ids)")
    p.add_argument("company", help="https://www.linkedin.com/company/<slug>/ or just <slug>")

    p = sub.add_parser("needs-browser", help="print whether any enabled source needs Playwright")
    _common(p)
    p.add_argument("--groups")

    args = parser.parse_args(argv)
    _load_secrets_json()
    if getattr(args, "verbose", False):
        log.set_verbose(True)
    data_dir = Path(getattr(args, "data_dir", "data"))

    try:
        if args.cmd in ("run", "dry-run", "bootstrap"):
            from .app import run

            return asyncio.run(run(
                args.config, data_dir,
                groups=_groups(args.groups),
                force=getattr(args, "force", False) or args.cmd == "dry-run",
                dry_run=getattr(args, "dry_run", False) or args.cmd == "dry-run",
                push=not getattr(args, "no_push", False),
                bootstrap=args.cmd == "bootstrap",
                skip_polling=getattr(args, "skip_polling", False),
            ))
        if args.cmd == "validate-config":
            return _validate(args.config, data_dir)
        if args.cmd == "test-notify":
            from .app import test_notify

            return asyncio.run(test_notify(args.config, data_dir))
        if args.cmd == "test-source":
            from .tools import test_source

            return asyncio.run(test_source(args, data_dir))
        if args.cmd == "detect":
            from . import sources  # noqa: F401  (registers adapters)
            from .sources.detect import detect_url

            found = detect_url(args.url)
            print(f"{found[0]}: {found[1]}" if found else "unknown: set 'type' explicitly (json_api, html_list...)")
            return 0 if found else 1
        if args.cmd == "linkedin-id":
            from .tools import linkedin_company_id

            return asyncio.run(linkedin_company_id(args.company))
        if args.cmd == "needs-browser":
            from .tools import needs_browser

            return needs_browser(args.config, data_dir, _groups(args.groups))
    except ConfigError:
        return 2
    return 1


def _validate(config_path: str | None, data_dir: Path) -> int:
    from . import sources  # noqa: F401
    from .config.loader import load_config
    from .sources.base import AdapterError
    from .sources.detect import resolve

    try:
        config = load_config(config_path, data_dir)
    except ConfigError as exc:
        log.warn(exc.public)
        if not log.in_ci():
            print(exc.detail, file=sys.stderr)
        return 2
    problems = 0
    for i, s in enumerate(config.sources, 1):
        try:
            resolve(s)
        except AdapterError as exc:
            problems += 1
            log.warn(f"source #{i}: {exc}")
            log.detail(f"source #{i} is {s.name}")
    log.info(
        f"config OK: {len(config.groups)} groups, {len(config.sources)} sources "
        f"({sum(s.enabled for s in config.sources)} enabled, {sum(s.favorite for s in config.sources)} favourites), "
        f"{len(config.locations)} locations, {len(config.role_families)} role families, "
        f"AI {'on' if config.llm.enabled and config.llm.providers else 'off'}"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
