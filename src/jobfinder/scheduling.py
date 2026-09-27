"""Decide which notification groups are due.

The workflow can be woken up by GitHub's cron (often late), by an external
trigger (cron-job.org) or manually. Whatever woke it up, a group runs only when
its most recent slot has not been executed yet, so runs are idempotent and never
produce duplicate notifications.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config.schema import Config, Group
from .state import State, parse_iso


@dataclass
class DueGroup:
    name: str
    slot: datetime | None  # scheduled slot (UTC-aware), None for interval groups
    reason: str


def _parse_hhmm(value: str) -> time:
    h, m = value.strip().split(":")
    return time(int(h), int(m))


def in_quiet_hours(group: Group, now_local: datetime) -> bool:
    if not group.quiet_hours:
        return False
    start_s, end_s = group.quiet_hours.split("-")
    start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
    t = now_local.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # window crosses midnight


def latest_slot(group: Group, now: datetime, tz: ZoneInfo) -> datetime | None:
    """Most recent scheduled slot <= now (aware datetime), or None."""
    if not group.times:
        return None
    now_local = now.astimezone(tz)
    weekdays = group.weekday_numbers()
    best: datetime | None = None
    for days_back in range(0, 9):
        day = (now_local - timedelta(days=days_back)).date()
        if weekdays is not None and day.weekday() not in weekdays:
            continue
        for t in group.times:
            slot = datetime.combine(day, _parse_hhmm(t), tzinfo=tz)
            if slot <= now_local and (best is None or slot > best):
                best = slot
        if best is not None:
            break
    return best


def is_due(name: str, group: Group, state: State, now: datetime, tz: ZoneInfo) -> DueGroup | None:
    info = state.group_info(name)
    now_local = now.astimezone(tz)

    if group.interval_minutes:
        if in_quiet_hours(group, now_local):
            return None
        last = parse_iso(info.get("last_run"))
        # 2 minutes of tolerance so an external trigger firing every N minutes is never skipped.
        if last is None or now - last >= timedelta(minutes=group.interval_minutes) - timedelta(minutes=2):
            return DueGroup(name, None, "interval")
        return None

    slot = latest_slot(group, now, tz)
    if slot is None:
        return None
    if now - slot > timedelta(hours=group.grace_hours):
        return None
    last_slot = parse_iso(info.get("last_slot"))
    if last_slot is not None and last_slot >= slot:
        return None
    if group.every_days > 1 and last_slot is not None:
        elapsed_days = (slot.astimezone(tz).date() - last_slot.astimezone(tz).date()).days
        if elapsed_days < group.every_days:
            return None
    return DueGroup(name, slot, "slot")


def due_groups(
    config: Config,
    state: State,
    now: datetime,
    only: list[str] | None = None,
    force: bool = False,
) -> list[DueGroup]:
    tz = config.tz
    out: list[DueGroup] = []
    for name, group in config.groups.items():
        if only and name not in only:
            continue
        if force:
            out.append(DueGroup(name, latest_slot(group, now, tz), "forced"))
            continue
        due = is_due(name, group, state, now, tz)
        if due:
            out.append(due)
    return out
