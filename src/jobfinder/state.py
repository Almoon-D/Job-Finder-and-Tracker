"""Persistent state kept in the private data repository.

``state/seen.json``  – every job ever seen: first/last seen, where it was notified,
                       cached AI verdicts.
``state/runs.json``  – scheduling bookkeeping per group and health per source.
"""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Job, Verdict, canonical_url

PRUNE_AFTER_DAYS = 90
MAX_PERSISTED_STREAK = 5


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat(timespec="seconds") if dt else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class State:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "state"
        self.seen_path = self.dir / "seen.json"
        self.runs_path = self.dir / "runs.json"
        self.seen: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, Any] = {"groups": {}, "sources": {}, "monitors": {}}
        self._index: dict[str, set[str]] | None = None  # fingerprint / canonical URL -> job keys
        self._touched: dict[str, set[str]] = {"sources": set(), "monitors": set()}
        # True when something worth committing changed (new jobs, notifications, health changes...).
        self.material = False
        self._load()

    # ------------------------------------------------------------------ io
    def _load(self) -> None:
        if self.seen_path.exists():
            self.seen = json.loads(self.seen_path.read_text(encoding="utf-8")).get("jobs", {})
        if self.runs_path.exists():
            loaded = json.loads(self.runs_path.read_text(encoding="utf-8"))
            for k in ("groups", "sources", "monitors"):
                self.runs[k] = loaded.get(k, {})

    def save(self) -> None:
        self.prune()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.seen_path.write_text(
            json.dumps({"jobs": self.seen}, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.runs_path.write_text(
            json.dumps(self.runs, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )

    def merge_with_disk(self) -> None:
        """Merge the version currently on disk (e.g. pushed by a concurrent run) into memory."""
        other = State(self.dir.parent)
        for key, theirs in other.seen.items():
            ours = self.seen.get(key)
            if ours is None:
                self.seen[key] = theirs
                continue
            notified = dict(theirs.get("notified", {}))
            for g, t in ours.get("notified", {}).items():
                notified[g] = min(t, notified.get(g, t))
            ours["notified"] = notified
            ours["first_seen"] = min(ours.get("first_seen") or "~", theirs.get("first_seen") or "~")
            ours["last_seen"] = max(ours.get("last_seen") or "", theirs.get("last_seen") or "")
            if "verdict" not in ours and "verdict" in theirs:
                ours["verdict"] = theirs["verdict"]
            if theirs.get("also_on"):
                ours["also_on"] = list(dict.fromkeys(theirs["also_on"] + ours.get("also_on", [])))
        for g, theirs in other.runs["groups"].items():
            ours = self.runs["groups"].setdefault(g, {})
            for field_name in ("last_run", "last_slot"):
                vals = [v for v in (ours.get(field_name), theirs.get(field_name)) if v]
                if vals:
                    ours[field_name] = max(vals)
        for section in ("sources", "monitors"):
            for k, theirs in other.runs[section].items():
                if k not in self._touched[section]:
                    self.runs[section][k] = theirs
        self._index = None

    def prune(self, now: datetime | None = None) -> None:
        limit = (now or now_utc()) - timedelta(days=PRUNE_AFTER_DAYS)
        for k in [k for k, v in self.seen.items() if (parse_iso(v.get("last_seen")) or limit) < limit]:
            del self.seen[k]

    # ---------------------------------------------------------------- jobs
    def observe(self, job: Job, now: datetime, target: str | None = None) -> bool:
        """Record that a job was seen. Returns True if it is new."""
        entry = self.seen.get(job.key)
        is_new = entry is None
        if is_new:
            entry = {"first_seen": iso(now), "notified": {}}
            self.seen[job.key] = entry
            self.material = True
        last = entry.get("last_seen") or ""
        if last[:10] != (iso(now) or "")[:10]:  # refresh at most once a day to keep commits small
            entry["last_seen"] = iso(now)
        entry.update(
            {
                "title": job.title,
                "company": job.company,
                "url": job.url,
                "loc": job.location_text,
                "posted_at": iso(job.posted_at),
                "fp": job.fingerprint,
                "src": job.source_key,
            }
        )
        if target:
            entry["tgt"] = target
        job.first_seen = parse_iso(entry["first_seen"])
        return is_new

    def mark_baseline(self, job: Job) -> None:
        """Jobs already listed when a source was first added: never notified unless dated and fresh."""
        entry = self.seen.get(job.key)
        if entry is not None and not entry.get("baseline"):
            entry["baseline"] = True
            self.material = True

    def is_baseline(self, job: Job) -> bool:
        return bool(self.seen.get(job.key, {}).get("baseline"))

    def notified_in(self, job: Job, group: str) -> bool:
        return group in self.seen.get(job.key, {}).get("notified", {})

    def notified_groups(self, job: Job) -> set[str]:
        return set(self.seen.get(job.key, {}).get("notified", {}))

    def _add_to_index(self, key: str, entry: dict[str, Any]) -> None:
        assert self._index is not None
        for token in (f"fp:{entry.get('fp')}", f"url:{canonical_url(entry.get('url') or '')}"):
            if not token.endswith(":") and not token.endswith(":None"):
                self._index.setdefault(token, set()).add(key)

    def notified_duplicates(self, job: Job, target: str | None = None) -> dict[str, set[str]]:
        """Other jobs already notified that look like the same offer: {key: groups it was notified in}.

        Same canonical URL, or same fuzzy fingerprint in a compatible place (the same configured
        location, or one of them without a known place).
        """
        if self._index is None:
            self._index = {}
            for k, v in self.seen.items():
                if v.get("notified"):
                    self._add_to_index(k, v)
        by_url = self._index.get(f"url:{job.canonical_url}", set()) if job.canonical_url else set()
        by_fp = self._index.get(f"fp:{job.fingerprint}", set())
        out: dict[str, set[str]] = {}
        for k in (by_url | by_fp) - {job.key}:
            entry = self.seen.get(k, {})
            groups = set(entry.get("notified", {}))
            if not groups:
                continue
            other = entry.get("tgt")
            if k not in by_url and target and other and other != target:
                continue
            out[k] = groups
        return out

    def add_also_on(self, key: str, name: str) -> None:
        entry = self.seen.get(key)
        if entry is not None and name not in entry.setdefault("also_on", []):
            entry["also_on"].append(name)
            self.material = True

    def mark_notified(self, job: Job, group: str, now: datetime) -> None:
        entry = self.seen.setdefault(job.key, {"first_seen": iso(now), "notified": {}})
        entry.setdefault("notified", {})[group] = iso(now)
        self.material = True
        entry["fp"] = job.fingerprint
        entry.setdefault("url", job.url)
        if self._index is not None:
            self._add_to_index(job.key, entry)
        if job.score is not None:
            entry["score"] = job.score

    def cached_verdict(self, job: Job, criteria_hash: str) -> Verdict | None:
        v = self.seen.get(job.key, {}).get("verdict")
        if v and v.get("criteria") == criteria_hash:
            return Verdict.from_dict(v)
        return None

    def store_verdict(self, job: Job, criteria_hash: str, verdict: Verdict) -> None:
        entry = self.seen.get(job.key)
        if entry is not None:
            entry["verdict"] = {**verdict.to_dict(), "criteria": criteria_hash}
            self.material = True

    def recent_notified(self, since: datetime) -> list[dict[str, Any]]:
        out = []
        for k, v in self.seen.items():
            times = [parse_iso(t) for t in v.get("notified", {}).values()]
            times = [t for t in times if t]
            if times and max(times) >= since:
                out.append({"key": k, **v, "_notified_at": max(times)})
        out.sort(key=lambda e: e["_notified_at"], reverse=True)
        return out

    # -------------------------------------------------------------- groups
    def group_info(self, group: str) -> dict[str, Any]:
        return self.runs["groups"].setdefault(group, {})

    def mark_group_run(self, group: str, now: datetime, slot: datetime | None) -> None:
        info = self.group_info(group)
        info["last_run"] = iso(now)
        if slot is not None:
            info["last_slot"] = iso(slot)

    # ------------------------------------------------------------- sources
    def source_info(self, key: str) -> dict[str, Any]:
        self._touched["sources"].add(key)
        return self.runs["sources"].setdefault(key, {})

    def is_bootstrapped(self, key: str) -> bool:
        return bool(self.source_info(key).get("bootstrapped"))

    def set_bootstrapped(self, key: str) -> None:
        info = self.source_info(key)
        if not info.get("bootstrapped"):
            info["bootstrapped"] = True
            self.material = True

    def record_source_result(self, key: str, ok: bool, count: int, error: str | None, now: datetime) -> None:
        info = self.source_info(key)
        previous = int(info.get("fail_streak", 0))
        # Recovering, or failing for the first few times, is worth committing even in polling runs;
        # after that the streak stops growing on disk so a broken source does not commit every 10 min.
        if (ok and previous) or (not ok and previous < MAX_PERSISTED_STREAK):
            self.material = True
        if ok:
            info["fail_streak"] = 0
            info["last_ok"] = iso(now)
            info.pop("last_error", None)
            counts = info.setdefault("counts", [])
            counts.append(count)
            del counts[:-10]
        else:
            info["fail_streak"] = int(info.get("fail_streak", 0)) + 1
            info["last_error"] = (error or "error")[:300]

    def health_problems(self, keys: list[str], fail_threshold: int) -> list[tuple[str, str]]:
        """Sources failing repeatedly or suddenly returning zero results."""
        out = []
        for key in keys:
            info = self.runs["sources"].get(key, {})
            streak = int(info.get("fail_streak", 0))
            if streak >= fail_threshold:
                out.append((key, f"fail x{streak}: {info.get('last_error', '')[:120]}"))
                continue
            counts = info.get("counts", [])
            if len(counts) >= 4 and counts[-1] == 0 and counts[-2] == 0 and statistics.median(counts[:-2]) > 0:
                out.append((key, "zero"))
        return out

    # ------------------------------------------------------------ monitors
    def monitor(self, key: str) -> dict[str, Any]:
        self._touched["monitors"].add(key)
        return self.runs["monitors"].setdefault(key, {})
