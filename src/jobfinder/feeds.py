"""Private feeds for dashboards like Glance or Homepage (see docs/DASHBOARDS.md).

``feeds/jobs.json``    – JSON Feed 1.1 of the jobs notified in the last 30 days
``feeds/jobs.xml``     – the same as RSS 2.0
``feeds/summary.json`` – counts, source health, tracker counts and the latest jobs
``feeds/tracker.json`` – the application tracker (only when the tracker is on)

They are written to the PRIVATE data repository and read by dashboards through the GitHub
contents API with a read-only token.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .config.schema import Config
from .i18n import t
from .state import State, iso, parse_iso
from .tracker.buttons import job_id
from .tracker.store import STATUSES, Tracker, status_label

FEED_DAYS = 30


def _items(config: Config, state: State, now: datetime, statuses: dict[str, str]) -> list[dict]:
    entries = state.recent_notified(now - timedelta(days=FEED_DAYS))[: config.feeds.max_items]
    families = {f.name: f.label or f.name.replace("_", " ").capitalize() for f in config.role_families}
    items = []
    for e in entries:
        groups = sorted(e.get("notified", {}))
        family = e.get("family") or (e.get("verdict") or {}).get("family")
        status = statuses.get(job_id(e["key"]))
        items.append({
            "id": e["key"],
            "url": e.get("url"),
            "title": f"{e.get('company', '')} — {e.get('title', '')}",
            "company": e.get("company"),
            "job_title": e.get("title"),
            "location": e.get("loc"),
            "posted_at": e.get("posted_at"),
            "first_seen": e.get("first_seen"),
            "notified_at": iso(e["_notified_at"]),
            "groups": groups,
            "family": families.get(family or "") if family else None,
            "score": e.get("score"),
            "reason": (e.get("verdict") or {}).get("reason"),
            "also_on": e.get("also_on") or [],
            "tracker_status": status,
            "tracker_label": status_label(status, config.language) if status else None,
            "lang": config.language,
        })
    return items


def _also(item: dict) -> str:
    return f"{t(item['lang'], 'also_on')}: {', '.join(item['also_on'])}" if item["also_on"] else ""


def _clean(value: Any) -> Any:
    """Drop null values and empty strings/lists (JSON Feed fields are optional, never null)."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _sources_health(config: Config, state: State) -> dict[str, Any]:
    enabled = {s.key: s.name for s in config.sources if s.enabled}
    info = {k: state.runs["sources"].get(k, {}) for k in enabled}
    problems = state.health_problems(list(enabled), config.notify.health_alert_after_failures)
    checked = [k for k, v in info.items() if v.get("last_ok") or v.get("fail_streak")]
    return {
        "total": len(enabled),
        "checked": len(checked),
        "ok": sum(1 for k in checked if not info[k].get("fail_streak")),
        "failing": sum(1 for k in checked if info[k].get("fail_streak")),
        "problems": [enabled[k] for k, _ in problems],
    }


def write_feeds(config: Config, state: State, data_dir: Path, now: datetime, last_runs: dict,
                tracker: Tracker | None = None) -> None:
    if not config.feeds.enabled:
        return
    out = Path(data_dir) / "feeds"
    out.mkdir(parents=True, exist_ok=True)
    lang = config.language
    statuses = tracker.statuses() if tracker else {}
    items = _items(config, state, now, statuses)

    def description(it: dict) -> str:
        return " · ".join(x for x in (it["location"], it["reason"], _also(it)) if x)

    json_feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": config.feeds.title,
        "description": f"{config.feeds.title}: {FEED_DAYS} days",
        "language": lang,
        "items": [
            {
                "id": it["id"],
                "url": it["url"],
                "title": it["title"],
                "content_text": description(it) or it["title"],
                "summary": it["location"],
                "date_published": it["posted_at"] or it["first_seen"],
                "date_modified": it["notified_at"],
                "authors": [{"name": it["company"]}] if it["company"] else None,
                "tags": it["groups"] + [x for x in (it["family"], it["tracker_label"]) if x],
                "_jobfinder": {k: it[k] for k in ("company", "job_title", "location", "score", "groups", "also_on",
                                                  "family", "tracker_status")},
            }
            for it in items
        ],
    }
    json_feed["items"] = [_clean(item) for item in json_feed["items"]]  # `items` itself is required
    _json(out / "jobs.json", json_feed)

    rss_items = []
    for it in items:
        date = parse_iso(it["notified_at"]) or now
        categories = it["groups"] + [x for x in (it["family"], it["tracker_label"]) if x]
        rss_items.append(
            "<item>"
            f"<title>{escape(it['title'])}</title>"
            f"<link>{escape(it['url'] or '')}</link>"
            f"<guid isPermaLink=\"false\">{escape(it['id'])}</guid>"
            f"<pubDate>{format_datetime(date)}</pubDate>"
            f"<description>{escape(description(it))}</description>"
            + "".join(f"<category>{escape(c)}</category>" for c in categories)
            + "</item>"
        )
    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
        f"<title>{escape(config.feeds.title)}</title><link>https://github.com</link>"
        f"<description>{escape(config.feeds.title)}</description>"
        f"<language>{escape(lang)}</language>"
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>"
        + "".join(rss_items)
        + "</channel></rss>\n"
    )
    (out / "jobs.xml").write_text(rss, encoding="utf-8")

    def count_since(days: float) -> int:
        limit = now - timedelta(days=days)
        return sum(1 for it in items if (parse_iso(it["notified_at"]) or now) >= limit)

    by_group: dict[str, int] = {}
    by_family: dict[str, int] = {}
    for it in items:
        for g in it["groups"]:
            by_group[g] = by_group.get(g, 0) + 1
        fam = it["family"] or t(lang, "other")
        by_family[fam] = by_family.get(fam, 0) + 1
    tracker_feed = tracker.feed(now, config.tracker.follow_up_days) if tracker else None
    summary = {
        "generated_at": iso(now),
        "language": lang,
        "jobs_last_24h": count_since(1),
        "jobs_last_7d": count_since(7),
        "jobs_last_30d": len(items),
        "by_group_30d": by_group,
        "by_family_30d": by_family,
        "last_runs": last_runs,
        "sources": _sources_health(config, state),
        "tracker": {
            "enabled": tracker is not None,
            "counts": tracker_feed["counts"] if tracker_feed else {s: 0 for s in STATUSES},
            "pending": tracker_feed["pending"] if tracker_feed else {"to_apply": 0, "follow_up": 0},
        },
        "latest": [
            {"title": it["title"], "company": it["company"] or "", "job_title": it["job_title"] or "",
             "url": it["url"] or "", "location": it["location"] or "", "notified_at": it["notified_at"],
             "tracker_status": it["tracker_status"] or "", "tracker_label": it["tracker_label"] or ""}
            for it in items[:10]
        ],
    }
    _json(out / "summary.json", summary)
    if tracker_feed is not None:
        _json(out / "tracker.json", tracker_feed)
