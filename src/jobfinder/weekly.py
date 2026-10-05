"""Weekly summary (groups with ``kind: summary``): what arrived, what was filtered, tracker and sources.

It is sent to every enabled channel and saved as ``reports/weekly/YYYY-Www.md`` in the private
data repository. Channels get a compact version; the Markdown file has the full tables.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config.schema import Config
from .i18n import t
from .notify.base import Report
from .state import State, parse_iso
from .stats import RunStats, sum_days
from .tracker.store import STATUSES, Tracker, parse_status, status_label

TOP_COMPANIES = 10
TOP_SOURCES = 5
MIN_MARKS = 5  # ✅/❌ answers needed before the acceptance table is shown
POSITIVE = {"interested", "applied", "interview", "offer", "rejected"}  # you wanted it; "discarded" is the ❌
SKIPPED_REASONS = {"already_notified", "baseline"}  # not decisions: the job was simply not new
REASON_KEYS = {"location": "r_location", "too_old": "r_too_old", "excluded": "r_excluded",
               "experience": "r_experience", "no_keyword": "r_no_keyword", "ai_score": "r_ai_score",
               "source_family": "r_source_family", "ai_unavailable": "r_ai_unavailable",
               "duplicate": "r_duplicate", "duplicate_previous": "r_duplicate", "duplicate_other_group": "r_duplicate"}


def _feedback(tracker: Tracker, state: State, key: str) -> dict[str, list[int]]:
    """{source or family: [wanted, discarded]} from the jobs you marked (only rows the tool sent)."""
    out: dict[str, list[int]] = {}
    for jid, status in tracker.statuses().items():
        entry = state.seen.get(tracker.messages.get(jid, {}).get("key") or "")
        if not entry:
            continue
        name = entry.get("src") if key == "src" else entry.get("family") or (entry.get("verdict") or {}).get("family")
        if name:
            out.setdefault(name, [0, 0])[0 if status in POSITIVE else 1] += 1
    return out


@dataclass
class WeeklyReport:
    week: str  # 2026-W39
    path: str  # reports/weekly/2026-W39.md (relative to the data dir)
    report: Report  # what the channels send
    markdown: str  # the file
    total: int  # new jobs in the week


def iso_week(when: datetime) -> str:
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


def _joined(counter: Counter, top: int | None = None, lang: str = "en") -> str:
    items = counter.most_common()
    shown = items[:top] if top else items
    text = " · ".join(f"{name} {n}" for name, n in shown)
    rest = sum(n for _, n in items[len(shown):])
    return f"{text} · {t(lang, 'w_others', n=rest)}" if rest else text


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out + [""]


def build_weekly(config: Config, state: State, tracker: Tracker | None, data_dir: Path, end: datetime,
                 label: str, group_labels: dict[str, str], current: RunStats | None = None) -> WeeklyReport:
    """Summary of the 7 days before ``end`` (the group's slot). ``current``: this run's unsaved figures."""
    lang, tz = config.language, config.tz
    end_local = end.astimezone(tz)
    start = end - timedelta(days=7)
    last_day = end_local.date()
    first_day = last_day - timedelta(days=6)
    week = iso_week(end_local)
    path = f"reports/weekly/{week}.md"
    title = f"📊 {label} {week}"
    none = t(lang, "w_none")

    # ---------------------------------------------------------------- new jobs
    by_group: Counter = Counter()
    by_company: Counter = Counter()
    by_family: Counter = Counter()
    families = {f.name: f.label or f.name.replace("_", " ").capitalize() for f in config.role_families}
    total = 0
    for entry in state.seen.values():
        if entry.get("dup_of"):
            continue  # folded into the job that was actually sent
        groups = [g for g, ts in entry.get("notified", {}).items() if (d := parse_iso(ts)) and start <= d < end]
        if not groups:
            continue
        total += 1
        for g in groups:
            by_group[group_labels.get(g, g)] += 1
        by_company[entry.get("company") or "?"] += 1
        fam = entry.get("family") or (entry.get("verdict") or {}).get("family")
        by_family[families.get(fam or "", t(lang, "other"))] += 1
    new_lines = []
    if total:
        new_lines = [f"{t(lang, 'w_by_group')}: {_joined(by_group)}",
                     f"{t(lang, 'w_by_company')}: {_joined(by_company, TOP_COMPANIES, lang)}",
                     f"{t(lang, 'w_by_family')}: {_joined(by_family)}"]

    # --------------------------------------------------- matches and discards
    stats = sum_days(data_dir, first_day, last_day, current)
    totals: Counter = Counter()
    reasons: Counter = Counter()
    for g in stats["groups"].values():
        for f in ("fetched", "candidates", "matches"):
            totals[f] += g.get(f, 0)
        for reason, n in g.get("rejected", {}).items():
            if reason not in SKIPPED_REASONS:
                reasons[t(lang, REASON_KEYS[reason]) if reason in REASON_KEYS else reason] += n
    funnel_lines = [none]
    if stats["groups"]:
        funnel_lines = [t(lang, "w_reviewed", matches=totals["matches"], fetched=totals["fetched"],
                          candidates=totals["candidates"])]
        if reasons:
            funnel_lines.append(f"{t(lang, 'w_discards')}: {_joined(reasons)}")

    fb_source: dict[str, list[int]] = {}
    fb_family: dict[str, list[int]] = {}
    if tracker is not None:
        fb_source, fb_family = _feedback(tracker, state, "src"), _feedback(tracker, state, "family")
        if sum(sum(v) for v in fb_source.values()) < MIN_MARKS:
            fb_source, fb_family = {}, {}

    # ---------------------------------------------------------------- tracker
    tracker_lines: list[str] = []
    counts: dict[str, int] = {}
    moves: Counter = Counter()
    to_apply: list = []
    follow: list = []
    if tracker is not None:
        counts = tracker.counts()
        for day, status, _row in tracker.changes():
            if first_day.isoformat() <= day <= last_day.isoformat():
                moves[status_label(parse_status(status) or status, lang)] += 1
        to_apply, follow = tracker.pending(end_local.date(), config.tracker.follow_up_days)
        if counts:
            tracker_lines.append(" · ".join(f"{status_label(s, lang)} {counts[s]}"
                                            for s in (*STATUSES, "other") if s in counts))
            if moves:
                tracker_lines.append(f"{t(lang, 'w_moves')}: " + " · ".join(f"+{n} {s}" for s, n in moves.items()))
            tracker_lines.append(f"{t(lang, 'pending_apply', n=len(to_apply))} · "
                                 f"{t(lang, 'pending_follow', n=len(follow), days=config.tracker.follow_up_days)}")
            if fb_family:
                labels = {f.name: f.label or f.name for f in config.role_families}
                tracker_lines.append(f"{t(lang, 'w_accept')}: " + " · ".join(
                    f"{labels.get(k, k)} {v[0]}/{sum(v)}" for k, v in sorted(fb_family.items())))
        else:
            tracker_lines.append(t(lang, "tracker_empty"))

    # ---------------------------------------------------------------- sources
    enabled = [s for s in config.sources if s.enabled]
    names = {s.key: s.name for s in enabled}
    problems = dict(state.health_problems(list(names), config.notify.health_alert_after_failures))
    zero = t(lang, "zero")
    ran = [k for k in names if stats["sources"].get(k, {}).get("runs")]
    ok_now = [k for k in ran if not state.runs["sources"].get(k, {}).get("fail_streak")]
    matches_by_source = Counter({names[k]: stats["sources"][k].get("matches", 0) for k in names
                                 if stats["sources"].get(k, {}).get("matches")})
    source_lines = [t(lang, "w_sources_ok", ok=len(ok_now), total=len(ran))] if ran else [none]
    if matches_by_source:
        source_lines.append(f"{t(lang, 'w_top')}: {_joined(matches_by_source, TOP_SOURCES, lang)}")
    if problems:
        source_lines.append(f"⚠️ {t(lang, 'w_problems')}: " + "; ".join(
            f"{names[k]} ({zero if p == 'zero' else p})" for k, p in problems.items()))

    sections = [(t(lang, "w_new", n=total), new_lines or [none]),
                (t(lang, "w_funnel"), funnel_lines)]
    if tracker is not None:
        sections.append((t(lang, "w_tracker"), tracker_lines))
    sections.append((t(lang, "w_sources"), source_lines))
    report = Report(title=title, sections=sections, footer=[t(lang, "w_report", path=path)])

    # --------------------------------------------------------------- markdown
    md = [f"# {title}", "", f"_{start.astimezone(tz):%d/%m/%Y %H:%M} – {end_local:%d/%m/%Y %H:%M}_", ""]
    md += [f"## {t(lang, 'w_new', n=total)}", ""]
    for heading, counter in ((t(lang, "w_by_group"), by_group), (t(lang, "w_by_company"), by_company),
                             (t(lang, "w_by_family"), by_family)):
        if counter:
            md += [f"### {heading}", ""] + _table([heading, t(lang, "w_col_count")],
                                                  [[k, n] for k, n in counter.most_common()])
    md += [f"## {t(lang, 'w_funnel')}", "", *[f"- {line}" for line in funnel_lines[:1]], ""]
    if reasons:
        md += _table([t(lang, "w_discards"), t(lang, "w_col_count")], [[k, n] for k, n in reasons.most_common()])
    if tracker is not None:
        md += [f"## {t(lang, 'w_tracker')}", ""]
        if counts:
            md += _table([t(lang, "w_tracker"), t(lang, "w_col_count"), t(lang, "w_moves")],
                         [[status_label(s, lang), counts[s], moves.get(status_label(s, lang), 0)]
                          for s in (*STATUSES, "other") if s in counts])
            md += [f"- {t(lang, 'pending_apply', n=len(to_apply))}",
                   f"- {t(lang, 'pending_follow', n=len(follow), days=config.tracker.follow_up_days)}", ""]
            if fb_source:
                src_names = {s.key: s.name for s in config.sources}
                md += [f"### {t(lang, 'w_accept')}", ""] + _table(
                    [t(lang, "w_col_source"), t(lang, "w_col_wanted"), t(lang, "w_col_discarded")],
                    [[src_names.get(k, k), v[0], v[1]] for k, v in sorted(fb_source.items(), key=lambda kv: -sum(kv[1]))])
        else:
            md += [t(lang, "tracker_empty"), ""]
    md += [f"## {t(lang, 'w_sources')}", "", f"- {source_lines[0]}", ""]
    rows = []
    for key in names:
        s = stats["sources"].get(key, {})
        if not s.get("runs") and key not in problems:
            continue
        runs = s.get("runs", 0)
        health = problems.get(key, "OK")
        rows.append([names[key], runs, s.get("ok", 0), round(s.get("jobs", 0) / runs, 1) if runs else 0,
                     s.get("matches", 0), round(s.get("seconds", 0) / runs, 1) if runs else 0,
                     zero if health == "zero" else health])
    rows.sort(key=lambda r: (-r[4], r[0].lower()))
    if rows:
        md += _table([t(lang, k) for k in ("w_col_source", "w_col_runs", "w_col_ok", "w_col_jobs",
                                           "w_col_matches", "w_col_seconds", "w_col_health")], rows)
    return WeeklyReport(week, path, report, "\n".join(md).rstrip() + "\n", total)
