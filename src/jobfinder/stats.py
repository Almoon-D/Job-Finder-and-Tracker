"""Run statistics accumulated per day in ``runs/stats.json`` (private repo), for the weekly summary.

Each run adds the numbers of its ``runs/last_run.json`` (per group) and per-source figures to the
bucket of its local day. The file is read, added to and written inside the persist step, so when a
push is rejected and the step runs again on top of the remote version, nothing is counted twice.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

KEEP_DAYS = 35
GROUP_FIELDS = ("fetched", "candidates", "matches", "ai_scored", "also_elsewhere")
SOURCE_FIELDS = ("runs", "ok", "jobs", "seconds", "matches")


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / "runs" / "stats.json"


def _add_group(into: dict[str, Any], other: dict[str, Any]) -> None:
    into["runs"] = into.get("runs", 0) + other.get("runs", 0)
    for f in GROUP_FIELDS:
        into[f] = into.get(f, 0) + other.get(f, 0)
    rejected = into.setdefault("rejected", {})
    for reason, n in other.get("rejected", {}).items():
        rejected[reason] = rejected.get(reason, 0) + n


def _add_source(into: dict[str, Any], other: dict[str, Any]) -> None:
    for f in SOURCE_FIELDS:
        into[f] = round(into.get(f, 0) + other.get(f, 0), 2)


class RunStats:
    """Figures of the current run, before they are added to runs/stats.json."""

    def __init__(self) -> None:
        self.groups: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, dict[str, Any]] = {}

    @property
    def empty(self) -> bool:
        return not self.groups and not self.sources

    def add_group(self, name: str, report: dict[str, Any], source_stats: dict[str, dict]) -> None:
        """``report``: the group's entry in last_run.json; ``source_stats``: GroupOutcome.source_stats."""
        _add_group(self.groups.setdefault(name, {}), {"runs": 1, **report})
        for key, s in source_stats.items():
            one = {"matches": s.get("matches", 0)}
            if "ok" in s:  # fetched in this group (a shared source is fetched once per run)
                one |= {"runs": 1, "ok": int(s["ok"]), "jobs": s.get("jobs", 0), "seconds": s.get("seconds", 0)}
            _add_source(self.sources.setdefault(key, {}), one)

    def as_day(self) -> dict[str, Any]:
        return {"groups": self.groups, "sources": self.sources}

    def merge_into(self, data_dir: Path, day: date) -> None:
        path = _path(data_dir)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        days = data.setdefault("days", {})
        bucket = days.setdefault(day.isoformat(), {"groups": {}, "sources": {}})
        for name, g in self.groups.items():
            _add_group(bucket["groups"].setdefault(name, {}), g)
        for key, s in self.sources.items():
            _add_source(bucket["sources"].setdefault(key, {}), s)
        oldest = (day - timedelta(days=KEEP_DAYS)).isoformat()
        data["days"] = {d: v for d, v in sorted(days.items()) if d > oldest}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def sum_days(data_dir: Path, first: date, last: date, extra: RunStats | None = None) -> dict[str, Any]:
    """Totals of the days first..last (inclusive), plus the current run's figures if given."""
    path = _path(data_dir)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    total: dict[str, Any] = {"groups": {}, "sources": {}, "days": 0}
    buckets = [v for d, v in data.get("days", {}).items() if first.isoformat() <= d <= last.isoformat()]
    total["days"] = len(buckets)
    if extra is not None and not extra.empty:
        buckets.append(extra.as_day())
    for bucket in buckets:
        for name, g in bucket.get("groups", {}).items():
            _add_group(total["groups"].setdefault(name, {}), g)
        for key, s in bucket.get("sources", {}).items():
            _add_source(total["sources"].setdefault(key, {}), s)
    return total
