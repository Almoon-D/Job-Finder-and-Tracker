from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobfinder.scheduling import due_groups, in_quiet_hours, is_due, latest_slot
from jobfinder.state import State

from .conftest import make_config


def _cfg(**groups):
    return make_config(groups=groups)


def test_slot_due_once(tmp_path):
    cfg = _cfg(company_sites={"times": ["09:00", "20:30"]})
    state = State(tmp_path)
    g = cfg.groups["company_sites"]
    now = datetime(2026, 9, 27, 7, 5, tzinfo=UTC)  # 09:05 Berlin (CEST, UTC+2)
    due = is_due("company_sites", g, state, now, cfg.tz)
    assert due and due.slot == datetime(2026, 9, 27, 7, 0, tzinfo=UTC)
    state.mark_group_run("company_sites", now, due.slot)
    # A late GitHub cron 40 minutes later must not run again
    assert is_due("company_sites", g, state, now + timedelta(minutes=40), cfg.tz) is None
    # Evening slot is due
    evening = datetime(2026, 9, 27, 18, 31, tzinfo=UTC)
    assert is_due("company_sites", g, state, evening, cfg.tz).slot.astimezone(UTC).hour == 18


def test_grace_window(tmp_path):
    cfg = _cfg(company_sites={"times": ["09:00"], "grace_hours": 3})
    state = State(tmp_path)
    g = cfg.groups["company_sites"]
    assert is_due("company_sites", g, state, datetime(2026, 9, 27, 9, 59, tzinfo=UTC), cfg.tz)
    assert is_due("company_sites", g, state, datetime(2026, 9, 27, 10, 1, tzinfo=UTC), cfg.tz) is None


def test_dst_winter(tmp_path):
    cfg = _cfg(company_sites={"times": ["09:00"]})
    state = State(tmp_path)
    # In winter Berlin is UTC+1: 09:00 local = 08:00 UTC
    slot = latest_slot(cfg.groups["company_sites"], datetime(2026, 12, 1, 8, 10, tzinfo=UTC), cfg.tz)
    assert slot.astimezone(UTC).hour == 8
    # 08:59 local: today's slot has not happened and yesterday's is outside the grace window
    assert is_due("company_sites", cfg.groups["company_sites"], state,
                  datetime(2026, 12, 1, 7, 59, tzinfo=UTC), cfg.tz) is None


def test_every_days(tmp_path):
    cfg = _cfg(boards={"times": ["11:00"], "every_days": 3})
    g = cfg.groups["boards"]
    state = State(tmp_path)
    day0 = datetime(2026, 9, 27, 9, 5, tzinfo=UTC)  # 11:05 Berlin
    d = is_due("boards", g, state, day0, cfg.tz)
    assert d
    state.mark_group_run("boards", day0, d.slot)
    for n in (1, 2):
        assert is_due("boards", g, state, day0 + timedelta(days=n), cfg.tz) is None
    assert is_due("boards", g, state, day0 + timedelta(days=3), cfg.tz)


def test_weekdays(tmp_path):
    cfg = _cfg(weekly={"times": ["18:00"], "weekdays": ["sun"], "kind": "summary"})
    g = cfg.groups["weekly"]
    state = State(tmp_path)
    sunday = datetime(2026, 9, 27, 16, 10, tzinfo=UTC)  # Sunday 18:10 Berlin
    assert is_due("weekly", g, state, sunday, cfg.tz)
    monday = datetime(2026, 9, 28, 16, 10, tzinfo=UTC)
    assert is_due("weekly", g, state, monday, cfg.tz) is None  # Sunday slot is outside grace


def test_interval_and_quiet_hours(tmp_path):
    cfg = _cfg(favorites={"interval_minutes": 10, "quiet_hours": "23:00-07:00"})
    g = cfg.groups["favorites"]
    state = State(tmp_path)
    t = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)  # 10:00 Berlin
    assert is_due("favorites", g, state, t, cfg.tz)
    state.mark_group_run("favorites", t, None)
    assert is_due("favorites", g, state, t + timedelta(minutes=5), cfg.tz) is None
    assert is_due("favorites", g, state, t + timedelta(minutes=9), cfg.tz)  # 2 min tolerance
    night = datetime(2026, 9, 27, 22, 30, tzinfo=UTC)  # 00:30 Berlin
    assert in_quiet_hours(g, night.astimezone(cfg.tz))
    assert is_due("favorites", g, state, night, cfg.tz) is None


def test_force_and_only(tmp_path):
    cfg = _cfg(a={"times": ["09:00"]}, b={"times": ["10:00"]})
    state = State(tmp_path)
    t = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
    assert due_groups(cfg, state, t) == []  # 05:00 Berlin: nothing due
    forced = due_groups(cfg, state, t, only=["b"], force=True)
    assert [d.name for d in forced] == ["b"]
