"""Tracker storage in the private data repository.

``tracker/applications.csv`` – one row per job you marked (by button or by hand). Editable on
                               the GitHub website: unknown columns, row order and ``notes`` are
                               kept; changing ``status`` by hand is recorded in ``history``.
``state/tracker.json``       – Telegram bookkeeping: the getUpdates offset and, for each job id,
                               the messages that carry its buttons (to keep them in sync).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..config.schema import Config
from ..i18n import t
from ..models import normalize_text

STATUSES = ("interested", "applied", "interview", "offer", "rejected", "discarded")
WORDS = {
    "es": dict(zip(STATUSES, ("interesa", "aplicado", "entrevista", "oferta", "rechazado", "descartado"), strict=True)),
    "en": dict(zip(STATUSES, STATUSES, strict=True)),
}
_EXTRA_WORDS = {
    "interested": ["interesada", "interesado", "me interesa", "interesting", "star", "starred"],
    "applied": ["aplicada", "aplicar", "candidatura", "enviada", "apply"],
    "interview": ["entrevistas", "interviewing", "interviews"],
    "offer": ["ofertada", "offered"],
    "rejected": ["rechazada", "rechazo", "reject"],
    "discarded": ["descartada", "descartar", "discard", "no interesa"],
}
_ALIASES = {normalize_text(w): s for words in WORDS.values() for s, w in words.items()}
_ALIASES |= {normalize_text(w): s for s, words in _EXTRA_WORDS.items() for w in words}

FIELDS = ("id", "status", "date", "company", "title", "location", "url", "notes", "history")
HEADERS = {
    "es": dict(zip(FIELDS, ("id", "estado", "fecha", "empresa", "puesto", "ubicacion", "url", "notas", "historial"),
                   strict=True)),
    "en": dict(zip(FIELDS, FIELDS, strict=True)),
}
_HEADER_ALIASES = {normalize_text(h): f for headers in HEADERS.values() for f, h in headers.items()}
_HEADER_ALIASES |= {"nota": "notes", "note": "notes", "position": "title", "role": "title", "cargo": "title",
                    "state": "status", "estatus": "status", "link": "url", "enlace": "url"}
INFO_FIELDS = ("company", "title", "location", "url")
REF_DAYS = 60
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MANUAL = "(manual)"


def _is_date(value: str | None) -> bool:
    """A real YYYY-MM-DD date (hand-typed values like 2026-09-31 are not)."""
    if not value or not _DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def parse_status(text: str | None) -> str | None:
    """'✅ Aplicada', 'applied', 'ENTREVISTA' → canonical status; unknown text → None."""
    return _ALIASES.get(normalize_text(text))


def status_label(status: str, lang: str) -> str:
    return t(lang, f"st_{status}") if status in STATUSES else status


def _split_history(row: dict[str, str]) -> list[str]:
    return [p.strip() for p in (row.get("history") or "").split(";") if p.strip()]


def _entry(entry: str) -> tuple[str | None, str]:
    """'2026-09-28 aplicado (manual)' → ('2026-09-28', 'aplicado')."""
    first, _, rest = entry.partition(" ")
    if not _is_date(first):
        first, rest = None, entry
    return first, rest.removesuffix(_MANUAL).strip()


def _same(a: str, b: str) -> bool:
    ca, cb = parse_status(a), parse_status(b)
    if ca or cb:
        return ca == cb
    return normalize_text(a) == normalize_text(b)


@dataclass
class TrackerOp:
    """A status change made with a Telegram button, re-applied on top of a concurrent version."""

    jid: str
    status: str
    date: str  # YYYY-MM-DD, local
    info: dict[str, str] = field(default_factory=dict)  # company, title, location, url


class Tracker:
    def __init__(self, data_dir: Path, lang: str, tz: ZoneInfo):
        self.data_dir = Path(data_dir)
        self.csv_path = self.data_dir / "tracker" / "applications.csv"
        self.state_path = self.data_dir / "state" / "tracker.json"
        self.lang = lang if lang in WORDS else "en"
        self.tz = tz
        self.ops: list[TrackerOp] = []
        self._load()

    # ------------------------------------------------------------------ io
    def _load(self) -> None:
        self.offset: int | None = None
        self.messages: dict[str, dict[str, Any]] = {}
        self.rows: list[dict[str, str]] = []
        self.headers: dict[str, str] = {}  # field -> header as written in the file
        self.extra: list[str] = []  # columns added by hand, kept in their order
        self._csv_dirty = self._state_dirty = False
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.offset = data.get("offset")
            self.messages = data.get("messages", {})
        if not self.csv_path.exists():
            return
        with self.csv_path.open(encoding="utf-8-sig", newline="") as fh:
            table = list(csv.reader(fh))
        if not table:
            return
        columns: list[str] = []
        for i, header in enumerate(table[0]):
            f = _HEADER_ALIASES.get(normalize_text(header))
            if f and f not in self.headers:
                self.headers[f] = header
                columns.append(f)
            else:
                name = f"x:{header or f'column{i + 1}'}"
                self.extra.append(name)
                columns.append(name)
        for values in table[1:]:
            if not any(v.strip() for v in values):
                continue
            row = {col: (v.strip() if not col.startswith("x:") else v) for col, v in zip(columns, values, strict=False)}
            self.rows.append(row)

    @property
    def changed(self) -> bool:
        return self._csv_dirty or self._state_dirty

    def save(self, now: datetime) -> None:
        if self._csv_dirty:
            header = [self.headers.get(f) or HEADERS[self.lang][f] for f in FIELDS] + [x[2:] for x in self.extra]
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            writer.writerow(header)
            for row in self.rows:
                writer.writerow([row.get(f, "") for f in FIELDS] + [row.get(x, "") for x in self.extra])
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            self.csv_path.write_text(buf.getvalue(), encoding="utf-8")
        if self._state_dirty:
            cutoff = (self.today(now) - timedelta(days=REF_DAYS)).isoformat()
            for jid in list(self.messages):
                refs = [r for r in self.messages[jid].get("refs", []) if r[2] >= cutoff]
                if refs:
                    self.messages[jid]["refs"] = refs
                else:
                    del self.messages[jid]
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"offset": self.offset, "messages": self.messages}
            self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8")

    def merge_with_disk(self, now: datetime) -> None:
        """Reload the version on disk (pushed by a concurrent run or edited on the web) and re-apply ours."""
        offset, messages = self.offset, self.messages
        self._load()
        if offset is not None and (self.offset is None or self.offset < offset):
            self.offset = offset
        for jid, mine in messages.items():
            theirs = self.messages.setdefault(jid, {"key": mine.get("key"), "refs": []})
            theirs["key"] = theirs.get("key") or mine.get("key")
            known = {(r[0], r[1]) for r in theirs["refs"]}
            theirs["refs"] += [r for r in mine.get("refs", []) if (r[0], r[1]) not in known]
        self._state_dirty = True
        self.detect_manual(self.today(now).isoformat())
        for op in self.ops:
            self.apply(op, record=False)

    def today(self, now: datetime) -> date:
        return now.astimezone(self.tz).date()

    # --------------------------------------------------------------- rows
    def find(self, jid: str) -> dict[str, str] | None:
        return next((r for r in self.rows if r.get("id") == jid), None)

    def status_of(self, jid: str) -> str | None:
        row = self.find(jid)
        return parse_status(row.get("status")) if row else None

    def statuses(self) -> dict[str, str]:
        """{job id: canonical status} of every row with a known status."""
        out = {}
        for row in self.rows:
            code = parse_status(row.get("status"))
            if row.get("id") and code:
                out[row["id"]] = code
        return out

    def apply(self, op: TrackerOp, record: bool = True) -> bool:
        """Set a job's status (idempotent). Returns True if the CSV changed."""
        if record:
            self.ops.append(op)
        row = self.find(op.jid)
        changed = False
        if row is None:
            row = {"id": op.jid}
            self.rows.append(row)
            changed = True
        for k in INFO_FIELDS:
            if not row.get(k) and op.info.get(k):
                row[k] = op.info[k]
                changed = True
        word = WORDS[self.lang][op.status]
        if parse_status(row.get("status")) != op.status:
            row["status"], row["date"] = word, op.date
            changed = True
        history = _split_history(row)
        if not history or not _same(_entry(history[-1])[1], row["status"]):
            history.append(f"{op.date} {word}")
            row["history"] = "; ".join(history)
            changed = True
        if changed:
            self._csv_dirty = True
        return changed

    def detect_manual(self, today: str) -> list[str]:
        """Rows whose status was changed (or added) by hand: record it in the history. Returns their ids."""
        out = []
        ids = {r.get("id") for r in self.rows}
        for row in self.rows:
            if not row.get("id"):
                if not any(row.get(k) for k in ("company", "title", "url")):
                    continue
                base = hashlib.sha1("|".join(row.get(k, "") for k in ("company", "title", "url")).encode()).hexdigest()
                jid, n = f"m-{base[:8]}", 1
                while jid in ids:
                    n += 1
                    jid = f"m-{base[:8]}-{n}"
                row["id"] = jid
                ids.add(jid)
                self._csv_dirty = True
            status = (row.get("status") or "").strip()
            history = _split_history(row)
            last_date, last = _entry(history[-1]) if history else (None, "")
            if not status or _same(status, last):
                continue
            when = (row.get("date") or "").strip()
            if not _is_date(when) or (last_date and when <= last_date):
                when = today
            if not _is_date(row.get("date")):
                row["date"] = when
            history.append(f"{when} {status} {_MANUAL}")
            row["history"] = "; ".join(history)
            self._csv_dirty = True
            out.append(row["id"])
        return out

    # --------------------------------------------------------- telegram
    def set_offset(self, offset: int) -> None:
        if offset != self.offset:
            self.offset = offset
            self._state_dirty = True

    def remember(self, jid: str, key: str | None, chat: str, message_id: int, day: str) -> None:
        """A message with this job's buttons was sent (or pressed)."""
        entry = self.messages.setdefault(jid, {"key": key, "refs": []})
        if key and entry.get("key") != key:
            entry["key"] = key
            self._state_dirty = True
        if all((r[0], r[1]) != (str(chat), int(message_id)) for r in entry["refs"]):
            entry["refs"].append([str(chat), int(message_id), day])
            self._state_dirty = True

    def refs(self, jid: str) -> list[tuple[str, int]]:
        return [(r[0], r[1]) for r in self.messages.get(jid, {}).get("refs", [])]

    # ------------------------------------------------------------ queries
    def counts(self) -> dict[str, int]:
        """Rows per canonical status ('other' for statuses typed by hand that are not known)."""
        c = Counter(parse_status(r.get("status")) or "other" for r in self.rows if (r.get("status") or "").strip())
        return {s: c[s] for s in (*STATUSES, "other") if c[s]}

    def changes(self) -> list[tuple[str, str, dict[str, str]]]:
        """Every history entry: (date, status text, row), oldest first."""
        out = []
        for row in self.rows:
            for entry in _split_history(row):
                day, status = _entry(entry)
                if day:
                    out.append((day, status, row))
        out.sort(key=lambda e: e[0])
        return out

    def last_change(self, row: dict[str, str]) -> str | None:
        history = _split_history(row)
        candidates = [_entry(history[-1])[0] if history else None, row.get("date")]
        dates = [d for d in candidates if _is_date(d)]
        return max(dates) if dates else None

    def pending(self, today: date, follow_up_days: int) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """(starred and not applied yet, applied without changes for follow_up_days or more)."""
        limit = (today - timedelta(days=follow_up_days)).isoformat()
        to_apply = [r for r in self.rows if parse_status(r.get("status")) == "interested"]
        follow = [r for r in self.rows if parse_status(r.get("status")) == "applied"
                  and (self.last_change(r) or "9999") <= limit]
        return to_apply, follow

    def feed(self, now: datetime, follow_up_days: int, limit: int = 200) -> dict[str, Any]:
        """Content of feeds/tracker.json."""
        today = self.today(now)
        counts = self.counts()
        to_apply, follow = self.pending(today, follow_up_days)
        items = []
        for row in self.rows:
            code = parse_status(row.get("status"))
            changed = self.last_change(row)
            items.append({
                "id": row.get("id", ""),
                "status": code or "other",
                "status_label": status_label(code, self.lang) if code else row.get("status", ""),
                "date": changed or "",
                "name": " — ".join(x for x in (row.get("company"), row.get("title")) if x) or row.get("id", ""),
                "company": row.get("company", ""),
                "title": row.get("title", ""),
                "location": row.get("location", ""),
                "url": row.get("url", ""),
                "notes": row.get("notes", ""),
                "days": (today - date.fromisoformat(changed)).days if changed else None,
            })
        items.sort(key=lambda i: i["date"], reverse=True)
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "language": self.lang,
            "labels": {s: status_label(s, self.lang) for s in STATUSES},
            "counts": {s: counts.get(s, 0) for s in (*STATUSES, "other")},
            "total": len(self.rows),
            "funnel": [{"status": s, "label": status_label(s, self.lang), "count": counts.get(s, 0)}
                       for s in STATUSES],
            "pending": {"to_apply": len(to_apply), "follow_up": len(follow)},
            "items": items[:limit],
        }


def open_tracker(config: Config, data_dir: Path) -> Tracker | None:
    """The tracker, when it is enabled and Telegram (the only channel with buttons) is on."""
    if not config.tracker.enabled or not config.notify.telegram.enabled:
        return None
    return Tracker(data_dir, config.language, config.tz)
