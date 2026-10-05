"""notify.routing: which chat gets which jobs."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from jobfinder.models import Job
from jobfinder.notify.base import Notification, Report
from jobfinder.notify.channels import footer_lines
from jobfinder.notify.routing import Router

from .conftest import NOW, make_config

TZ = ZoneInfo("Europe/Madrid")

ROUTING = {"summary": "Resumen", "highlights": "Destacadas", "other": "Otros",
           "places": {"Germany": "DACH", "Lisbon": "Lisboa"}}


def job(n: int, place: str | None, score: int | None = 70, source: str = "s", **extra) -> Job:
    j = Job(source, str(n), f"Role {n}", f"https://jobs.example.test/{n}", f"Company {n}", [place or ""])
    j.score = score
    j.extra["target"] = place
    j.extra.update(extra)
    return j


def config(routing: dict | None = None, favorite: str | None = None):
    sources = [{"name": "Fav", "id": "fav", "group": "company_sites", "type": "fake", "favorite": True}] if favorite else []
    return make_config(notify={"telegram": {"enabled": True}, "routing": ROUTING if routing is None else routing},
                       sources=sources)


def notification(jobs: list[Job], **kw) -> Notification:
    kw = {"format": "per_job", "buttons": True, "problems": [], "sources_ok": 3, "sources_total": 4, **kw}
    return Notification("company_sites", "Webs", jobs, "es", TZ, NOW, **kw)


def by_kind(plans):
    out: dict[str, list] = {}
    for p in plans:
        out.setdefault(p.kind, []).append(p)
    return out


def keys(plan) -> list[str]:
    return [j.key for j in plan.notification.jobs]


def test_not_routed_is_one_unchanged_notification():
    n = notification([job(1, "Lisbon")])
    plans = Router(config({})).plan(n)
    assert [(p.route, p.kind, p.notification) for p in plans] == [(None, "all", n)]


def test_every_job_goes_to_its_place_the_summary_and_other():
    jobs = [job(1, "Germany"), job(2, "Lisbon"), job(3, None), job(4, "Atlantis"), job(5, "Germany")]
    plans = Router(config()).plan(notification(jobs))
    routes = {(p.route, p.kind): keys(p) for p in plans}
    assert routes == {
        ("Resumen", "summary"): ["s:1", "s:2", "s:3", "s:4", "s:5"],
        ("DACH", "place"): ["s:1", "s:5"],
        ("Lisboa", "place"): ["s:2"],
        ("Otros", "other"): ["s:3", "s:4"],
    }
    assert [p.kind for p in plans][0] == "summary"  # the index first


def test_resumen_is_a_silent_compact_index_of_everything_with_the_health_report():
    jobs = [job(i, "Germany") for i in range(1, 41)]
    n = notification(jobs, max_items=10, problems=["Acme: 3 failures"], also_elsewhere=2, buttons=True)
    summary, dach = Router(config()).plan(n)[:2]
    s, d = summary.notification, dach.notification
    assert (s.format, s.buttons, s.silent, len(s.shown), s.hidden_count) == ("grouped", False, True, 40, 0)
    assert s.problems == ["Acme: 3 failures"] and s.show_sources and s.also_elsewhere == 2
    # the place chat keeps the group's own format and buttons, its own cap, no health noise
    assert (d.format, d.buttons, d.silent, d.max_items, d.hidden_count) == ("per_job", True, False, 10, 30)
    assert d.problems == [] and not d.show_sources and d.also_elsewhere == 0
    assert s.sources_line in footer_lines(s) and d.sources_line not in footer_lines(d)  # health only in the overview
    assert any("Acme" in line for line in footer_lines(s)) and not any("Acme" in line for line in footer_lines(d))


def test_places_share_a_chat_and_a_chat_never_gets_a_job_twice():
    cfg = config({"places": {"Germany": "DACH", "Lisbon": "DACH"}, "highlights": "DACH"})
    jobs = [job(1, "Germany", score=95), job(2, "Lisbon", score=50)]
    plans = [p for p in Router(cfg).plan(notification(jobs)) if p.kind != "summary"]
    # one chat, one message: the highlighted offer is not sent a second time as its place card
    assert [(p.route, p.kind, keys(p)) for p in plans] == [("DACH", "highlights", ["s:1", "s:2"])]
    shared = config({"places": {"Germany": "DACH", "Lisbon": "DACH"}})
    plans = [p for p in Router(shared).plan(notification(jobs)) if p.kind != "summary"]
    assert [(p.route, p.kind, keys(p)) for p in plans] == [("DACH", "place", ["s:1", "s:2"])]


@pytest.mark.parametrize("score,source,highlighted", [
    (85, "s", True), (84, "s", False), (None, "s", False), (10, "fav", True), (None, "fav", True)])
def test_highlights_are_favourite_sources_or_a_high_fit(score, source, highlighted):
    plans = by_kind(Router(config(favorite="fav")).plan(notification([job(1, "Lisbon", score, source)])))
    assert bool(plans.get("highlights")) is highlighted
    assert len(plans["place"]) == 1  # the same offer is always also in its place chat
    if highlighted:
        h = plans["highlights"][0].notification
        assert (h.format, h.priority, [j.key for j in h.jobs]) == ("per_job", "high", [f"{source}:1"])
        assert h.jobs[0].key == plans["place"][0].notification.jobs[0].key  # one offer, one key: one tracker id


def test_a_favourite_found_again_by_another_source_still_counts():
    kept = job(1, "Lisbon", 60, "board", duplicates=[job(2, "Lisbon", 60, "fav")])
    plans = by_kind(Router(config(favorite="fav")).plan(notification([kept])))
    assert plans["highlights"][0].notification.jobs == [kept]


def test_the_highlight_threshold_is_configurable():
    cfg = config({**ROUTING, "highlight_score": 60})
    assert by_kind(Router(cfg).plan(notification([job(1, "Lisbon", 60)])))["highlights"]


def test_without_a_summary_the_catch_all_chat_carries_the_health_report():
    cfg = config({"places": {"Germany": "DACH"}})  # no summary, no other: other = the default chat
    n = notification([job(1, "Germany"), job(2, None)], problems=["Acme: down"])
    plans = Router(cfg).plan(n)
    assert [(p.route, p.kind) for p in plans] == [("DACH", "place"), (None, "other")]
    dach, rest = (p.notification for p in plans)
    assert rest.problems == ["Acme: down"] and rest.show_sources and dach.problems == []


def test_warnings_get_a_chat_even_when_every_job_has_its_place():
    cfg = config({"places": {"Germany": "DACH"}})
    quiet = Router(cfg).plan(notification([job(1, "Germany")]))
    assert [p.kind for p in quiet] == ["place"]  # nothing to warn about: no empty message elsewhere
    plans = Router(cfg).plan(notification([job(1, "Germany")], problems=["Acme: down"]))
    assert [(p.route, p.kind, keys(p)) for p in plans] == [("DACH", "place", ["s:1"]), (None, "other", [])]
    assert plans[1].notification.problems == ["Acme: down"]


def test_reports_and_empty_notifications_only_go_to_the_overview():
    report = notification([], report=Report("Resumen semanal", [("A", ["x"])]))
    assert [(p.route, p.kind) for p in Router(config()).plan(report)] == [("Resumen", "summary")]
    empty = notification([], problems=["Acme: down"])
    assert [(p.route, p.kind) for p in Router(config()).plan(empty)] == [("Resumen", "summary")]
    no_summary = config({"places": {"Germany": "DACH"}, "other": "Otros"})
    assert [(p.route, p.kind) for p in Router(no_summary).plan(report)] == [("Otros", "other")]
    assert [(p.route, p.kind) for p in Router(config({"places": {"Germany": "DACH"}})).plan(report)] == [(None, "other")]


def test_a_test_goes_to_every_chat_and_to_the_default_one_when_other_is_not_named():
    n = notification([], test=True)
    named = Router(config()).plan(n)
    assert [p.route for p in named] == ["Resumen", "Destacadas", "DACH", "Lisboa", "Otros"]
    assert not named[0].notification.buttons and named[1].notification.buttons  # the tracker test needs no index
    assert [p.route for p in Router(config({"summary": "Resumen"})).plan(n)] == ["Resumen", None]


def test_place_names_are_matched_without_accents_or_case():
    cfg = make_config(locations=[{"country": "ES", "label": "España"}], notify={"routing": {"places": {"españa": "ES"}}})
    plans = Router(cfg).plan(notification([job(1, "España")]))
    assert [(p.route, p.kind) for p in plans] == [("ES", "place")]
