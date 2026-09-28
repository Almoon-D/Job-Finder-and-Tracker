"""Private feeds (JSON Feed 1.1, RSS 2.0, summary JSON) for dashboards like Glance or Homepage.

They are written to the PRIVATE data repository and read by dashboards through
the GitHub contents API with a read-only token (see docs/SETUP.es.md).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from .config.schema import Config
from .i18n import t
from .state import State, iso, parse_iso

FEED_DAYS = 30


def _items(config: Config, state: State, now: datetime) -> list[dict]:
    entries = state.recent_notified(now - timedelta(days=FEED_DAYS))[: config.feeds.max_items]
    items = []
    for e in entries:
        groups = sorted(e.get("notified", {}))
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
            "score": e.get("score"),
            "reason": (e.get("verdict") or {}).get("reason"),
            "also_on": e.get("also_on") or [],
            "lang": config.language,
        })
    return items


def _also(item: dict) -> str:
    return f"{t(item['lang'], 'also_on')}: {', '.join(item['also_on'])}" if item["also_on"] else ""


def write_feeds(config: Config, state: State, data_dir: Path, now: datetime, last_runs: dict) -> None:
    if not config.feeds.enabled:
        return
    out = Path(data_dir) / "feeds"
    out.mkdir(parents=True, exist_ok=True)
    items = _items(config, state, now)

    json_feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": config.feeds.title,
        "items": [
            {
                "id": it["id"],
                "url": it["url"],
                "title": it["title"],
                "content_text": " · ".join(x for x in (it["location"], it["reason"], _also(it)) if x) or it["title"],
                "date_published": it["posted_at"] or it["first_seen"],
                "date_modified": it["notified_at"],
                "tags": it["groups"],
                "_jobfinder": {k: it[k] for k in ("company", "job_title", "location", "score", "groups", "also_on")},
            }
            for it in items
        ],
    }
    (out / "jobs.json").write_text(json.dumps(json_feed, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    rss_items = []
    for it in items:
        date = parse_iso(it["notified_at"]) or now
        desc = " · ".join(x for x in (it["location"], it["reason"], _also(it)) if x)
        rss_items.append(
            "<item>"
            f"<title>{escape(it['title'])}</title>"
            f"<link>{escape(it['url'] or '')}</link>"
            f"<guid isPermaLink=\"false\">{escape(it['id'])}</guid>"
            f"<pubDate>{format_datetime(date)}</pubDate>"
            f"<description>{escape(desc)}</description>"
            "</item>"
        )
    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
        f"<title>{escape(config.feeds.title)}</title><link>https://github.com</link>"
        f"<description>{escape(config.feeds.title)}</description>"
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>"
        + "".join(rss_items)
        + "</channel></rss>\n"
    )
    (out / "jobs.xml").write_text(rss, encoding="utf-8")

    def count_since(days: float) -> int:
        limit = now - timedelta(days=days)
        return sum(1 for it in items if (parse_iso(it["notified_at"]) or now) >= limit)

    by_group: dict[str, int] = {}
    for it in items:
        for g in it["groups"]:
            by_group[g] = by_group.get(g, 0) + 1
    summary = {
        "generated_at": iso(now),
        "jobs_last_24h": count_since(1),
        "jobs_last_7d": count_since(7),
        "jobs_last_30d": len(items),
        "by_group_30d": by_group,
        "last_runs": last_runs,
        "latest": [{k: it[k] for k in ("title", "url", "location", "notified_at")} for it in items[:10]],
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
