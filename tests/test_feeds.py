"""Feeds for dashboards, and the examples in docs/DASHBOARDS.md against the files the tool writes."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml

from jobfinder.feeds import write_feeds
from jobfinder.models import Job
from jobfinder.state import State
from jobfinder.tracker import Tracker, TrackerOp, job_id

from .conftest import NOW, make_config

DOC = (Path(__file__).parent.parent / "docs" / "DASHBOARDS.md").read_text(encoding="utf-8")


@pytest.fixture
def feeds(tmp_path) -> Path:
    config = make_config(notify={"telegram": {"enabled": True}}, sources=[{"name": "S", "id": "s", "type": "fake"}])
    state = State(tmp_path)
    tracker = Tracker(tmp_path, "es", config.tz)
    for i in range(3):
        job = Job("s", str(i), f"Product Manager {i}", f"https://jobs.example.test/{i}", "Acme",
                  locations=["Berlin, Germany"], posted_at=NOW - timedelta(hours=i), posted_precision="datetime")
        job.family, job.score = "product", 80
        state.observe(job, NOW)
        state.mark_notified(job, "company_sites", NOW - timedelta(hours=i))
    state.observe(Job("s", "3", "No date", "https://jobs.example.test/3", ""), NOW)
    state.mark_notified(Job("s", "3", "No date", "https://jobs.example.test/3", ""), "favorites", NOW)
    tracker.apply(TrackerOp(job_id("s:0"), "applied", "2026-09-27", {"company": "Acme", "title": "PM 0",
                                                                    "url": "https://jobs.example.test/0"}))
    state.runs["sources"]["s"] = {"fail_streak": 0, "last_ok": NOW.isoformat()}
    write_feeds(config, state, tmp_path, NOW, {"company_sites": NOW.isoformat()}, tracker)
    return tmp_path / "feeds"


def _nulls(value) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_nulls(v) for v in value.values())
    if isinstance(value, list):
        return any(_nulls(v) for v in value)
    return False


def test_json_feed_1_1(feeds):
    feed = json.loads((feeds / "jobs.json").read_text())
    assert feed["version"] == "https://jsonfeed.org/version/1.1" and feed["title"] and feed["language"] == "es"
    assert not _nulls(feed)  # optional fields are omitted, never null
    for item in feed["items"]:
        assert item["id"] and (item.get("content_text") or item.get("content_html"))
        datetime.fromisoformat(item["date_published"])
    first = next(i for i in feed["items"] if i["id"] == "s:0")
    assert first["authors"] == [{"name": "Acme"}] and "✅ Aplicado" in first["tags"]
    assert first["_jobfinder"]["tracker_status"] == "applied" and first["_jobfinder"]["family"] == "Product"
    assert "authors" not in next(i for i in feed["items"] if i["id"] == "s:3")


def test_empty_json_feed_keeps_items(tmp_path):
    config = make_config()
    write_feeds(config, State(tmp_path), tmp_path, NOW, {})
    assert json.loads((tmp_path / "feeds" / "jobs.json").read_text())["items"] == []


def test_rss_and_summary(feeds):
    channel = ElementTree.fromstring((feeds / "jobs.xml").read_text()).find("channel")
    assert channel.findtext("language") == "es"
    item = channel.findall("item")[0]
    assert item.findtext("link").startswith("https://") and {c.text for c in item.findall("category")}
    summary = json.loads((feeds / "summary.json").read_text())
    assert summary["jobs_last_24h"] == 4 and summary["by_family_30d"] == {"Product": 3, "Otros puestos": 1}
    assert summary["tracker"]["counts"]["applied"] == 1 and summary["sources"]["ok"] == 1
    assert summary["latest"][0]["tracker_label"] == "✅ Aplicado"
    tracker = json.loads((feeds / "tracker.json").read_text())
    assert tracker["items"][0]["name"] == "Acme — PM 0" and tracker["funnel"][1]["count"] == 1


# ------------------------------------------------------------ DASHBOARDS.md
def _block(name: str) -> object:
    match = re.search(rf"<!-- dashboards-test: {name} -->\n```yaml\n(.*?)```", DOC, re.S)
    assert match, name
    return yaml.safe_load(match.group(1))


def _get(data: object, path: str) -> object:
    for part in path.split("."):
        if isinstance(data, list) and part.isdigit():
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            raise KeyError(path)
    return data


def _file(url: str, feeds: Path) -> dict | None:
    match = re.search(r"/contents/feeds/(\w+\.json)$", url)
    return json.loads((feeds / match.group(1)).read_text()) if match else None


def test_glance_examples_use_existing_fields(feeds):
    widgets = _block("glance")["pages"][0]["columns"][0]["widgets"]
    checked = 0
    for w in widgets:
        if w["type"] == "rss":
            assert all(f["headers"]["Accept"] == "application/vnd.github.raw+json" for f in w["feeds"])
            continue
        data = _file(w["url"], feeds)
        if data is None:  # the ntfy widget
            assert w["skip-json-validation"] is True
            continue
        assert w["headers"]["Accept"] == "application/vnd.github.raw+json"
        template = w["template"]
        arrays = re.findall(r'\.JSON\.Array "([^"]+)"', template)
        for path in re.findall(r'\.JSON\.(?:Int|String|Float|Bool|Exists|Array) "([^"]+)"', template):
            _get(data, path)
            checked += 1
        for key in re.findall(r'(?<!JSON)(?<![\w$])\.(?:String|Int|Float|Bool) "([^"]+)"', template):
            assert any(key in item for a in arrays for item in _get(data, a)), key
            checked += 1
    assert checked > 20


def test_homepage_examples_use_existing_fields(feeds):
    services = _block("homepage")[0]["Job finder"]
    for service in services:
        widget = next(iter(service.values()))["widget"]
        assert widget["headers"]["User-Agent"] and widget["headers"]["Accept"] == "application/vnd.github.raw+json"
        data = _file(widget["url"], feeds)
        mappings = widget["mappings"]
        if widget.get("display") == "dynamic-list":
            items = _get(data, mappings["items"])
            assert items
            for key in (mappings["name"], mappings["label"], *re.findall(r"\{(\w+)\}", mappings["target"])):
                assert key in items[0], key
        else:
            for m in mappings:
                _get(data, m["field"])
