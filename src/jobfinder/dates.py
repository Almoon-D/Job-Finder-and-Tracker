"""Parse the many date formats job sites use (absolute, epoch, relative, multilingual)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .models import DatePrecision

_REL_EN = re.compile(r"(\d+)\+?\s*(minute|min|hour|hr|day|week|month)s?\s*ago", re.I)
_REL_ES = re.compile(r"hace\s+(\d+)\+?\s*(minuto|hora|d[ií]a|semana|mes)(?:e?s)?", re.I)
_REL_FR = re.compile(r"il y a\s+(\d+)\+?\s*(minute|heure|jour|semaine|mois)s?", re.I)
_UNIT_HOURS = {
    "minute": 1 / 60, "min": 1 / 60, "minuto": 1 / 60,
    "hour": 1, "hr": 1, "hora": 1, "heure": 1,
    "day": 24, "dia": 24, "día": 24, "jour": 24,
    "week": 168, "semana": 168, "semaine": 168,
    "month": 720, "mes": 720, "mois": 720,
}
_NUMERIC = re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})$")
_TODAY = re.compile(r"\b(today|just posted|hoy|aujourd'hui|heute)\b", re.I)
_YESTERDAY = re.compile(r"\b(yesterday|ayer|hier|gestern)\b", re.I)


def parse_relative(text: str, now: datetime) -> datetime | None:
    """'Posted 3 Days Ago', 'hace 2 días', 'il y a 5 jours', 'Posted Today', '30+ days ago'."""
    if not text:
        return None
    if _TODAY.search(text):
        return now
    if _YESTERDAY.search(text):
        return now - timedelta(days=1)
    for rx in (_REL_EN, _REL_ES, _REL_FR):
        m = rx.search(text)
        if m:
            unit = m.group(2).lower()
            hours = _UNIT_HOURS.get(unit) or _UNIT_HOURS.get(unit.rstrip("s"), 24)
            return now - timedelta(hours=int(m.group(1)) * hours)
    return None


def parse_date(value: object, now: datetime | None = None) -> tuple[datetime | None, DatePrecision]:
    """Best-effort parse of a date value. Returns an aware UTC datetime and its precision."""
    now = now or datetime.now(UTC)
    if value is None or value == "":
        return None, "unknown"
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC), "datetime"
    if isinstance(value, int | float):
        ts = float(value)
        if ts > 1e12:  # milliseconds
            ts /= 1000
        if ts <= 0:
            return None, "unknown"
        return datetime.fromtimestamp(ts, UTC), "datetime"
    text = str(value).strip()
    if text.isdigit() and len(text) >= 9:
        return parse_date(int(text), now)
    rel = parse_relative(text, now)
    if rel is not None:
        return rel, "relative"
    m = _NUMERIC.match(text)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        day, month = (b, a) if (a <= 12 < b) else (a, b)  # day-first unless impossible
        try:
            return datetime(y, month, day, tzinfo=UTC), "date"
        except ValueError:
            return None, "unknown"
    iso_text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_text)
        precision: DatePrecision = "date" if len(text) <= 10 else "datetime"
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC), precision
    except ValueError:
        pass
    try:
        import dateparser

        dt = dateparser.parse(
            text,
            settings={"RETURN_AS_TIMEZONE_AWARE": True, "TIMEZONE": "UTC", "RELATIVE_BASE": now.replace(tzinfo=None)},
        )
    except Exception:  # dateparser raises assorted errors on garbage input
        dt = None
    if dt is None:
        return None, "unknown"
    has_time = bool(re.search(r"\d{1,2}:\d{2}", text))
    return dt.astimezone(UTC), "datetime" if has_time else "date"
