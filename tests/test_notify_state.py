"""Notification rendering, channel payloads, AI parsing and state merging."""

from __future__ import annotations

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from jobfinder.matching.llm import parse_results
from jobfinder.models import Job
from jobfinder.notify.base import Notification
from jobfinder.notify.channels import Discord, Email, Ntfy, Telegram, chunk
from jobfinder.sources.http import Http
from jobfinder.state import State

from .conftest import NOW, make_config

TZ = ZoneInfo("Europe/Berlin")


def _jobs(n: int) -> list[Job]:
    out = []
    for i in range(n):
        j = Job("s", str(i), f"Product Manager <{i}> & co", f"https://jobs.example.test/{i}?a=1&b=2", "Acme",
                locations=["Berlin, Germany"], posted_at=NOW - timedelta(hours=2), posted_precision="datetime")
        j.score, j.reason, j.experience = 80, "Encaja", "3-5"
        out.append(j)
    return out


def test_chunking():
    parts = ["a" * 30] * 10
    msgs = chunk(parts, 100)
    assert all(len(m) <= 100 for m in msgs) and sum(m.count("a") for m in msgs) == 300


def test_when_formats():
    n = Notification("g", "Webs", [], "es", TZ, NOW)
    j = _jobs(1)[0]
    assert n.when(j) == "Publicada 27/09 07:30"
    j.posted_precision = "relative"
    j.posted_at = NOW
    assert "hoy" in n.when(j)
    j.posted_at, j.first_seen = None, NOW
    assert n.when(j).startswith("Detectada 27/09")


@respx.mock
async def test_telegram_digest_escapes_and_chunks(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    route = respx.post("https://api.telegram.org/bott/sendMessage").mock(return_value=httpx.Response(200, json={}))
    cfg = make_config(notify={"telegram": {"enabled": True}})
    n = Notification("g", "Webs", _jobs(60), "es", TZ, NOW, max_items=60)
    async with Http() as http:
        await Telegram(cfg, http).send(n)
    texts = [json.loads(c.request.content)["text"] for c in route.calls]
    assert len(texts) > 1 and all(len(t) <= 4000 for t in texts)
    assert "&lt;0&gt; &amp; co" in texts[0] and "a=1&amp;b=2" in texts[0]


@respx.mock
async def test_discord_batches_embeds(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example.test/hook")
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    route = respx.post("https://discord.example.test/hook").mock(return_value=httpx.Response(204))
    cfg = make_config(notify={"discord": {"enabled": True}})
    async with Http() as http:
        await Discord(cfg, http).send(Notification("g", "Webs", _jobs(23), "es", TZ, NOW))
    payloads = [json.loads(c.request.content) for c in route.calls]
    assert [len(p["embeds"]) for p in payloads] == [10, 10, 3]
    assert "content" in payloads[0] and "footer" in payloads[-1]["embeds"][-1]


@respx.mock
async def test_ntfy_json_publish(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "my-secret-topic")
    route = respx.post("https://ntfy.sh").mock(return_value=httpx.Response(200, json={}))
    cfg = make_config(notify={"ntfy": {"enabled": True}})
    async with Http() as http:
        await Ntfy(cfg, http).send(Notification("g", "Favoritas", _jobs(2), "es", TZ, NOW, priority="high",
                                                format="per_job"))
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert len(bodies) == 2 and bodies[0]["priority"] == 5 and bodies[0]["topic"] == "my-secret-topic"
    assert bodies[0]["click"].startswith("https://jobs.example.test/")


def test_email_render():
    cfg = make_config(notify={"email": {"enabled": True, "to": ["me@example.test"]}})
    text, html = Email(cfg, None).render(Notification("g", "Webs", _jobs(2), "es", TZ, NOW))
    assert "https://jobs.example.test/0?a=1&b=2" in text
    assert "&lt;0&gt;" in html and 'href="https://jobs.example.test/0?a=1&amp;b=2"' in html


def test_parse_results_variants():
    assert parse_results('{"results": [{"id": "0", "score": 5}]}')[0]["score"] == 5
    assert parse_results('```json\n{"results": []}\n```') == []
    assert parse_results('Sure! {"results": [{"id": "1"}]} hope it helps')[0]["id"] == "1"
    with pytest.raises(ValueError):
        parse_results("no json here")


def test_state_merge_with_concurrent_run(tmp_path):
    job = Job("s", "1", "PB", "https://x", "Acme")
    other = Job("s", "2", "IR", "https://y", "Acme")
    a = State(tmp_path)
    b = State(tmp_path)  # a concurrent run that started from the same (empty) state
    a.observe(job, NOW)
    a.mark_notified(job, "favorites", NOW)
    a.mark_group_run("favorites", NOW, None)
    a.save()
    b.observe(other, NOW)
    b.mark_notified(other, "company_sites", NOW + timedelta(minutes=1))
    b.mark_group_run("company_sites", NOW, NOW)
    # meanwhile "a" pushed first; b merges on top of what is on disk
    b.merge_with_disk()
    b.save()
    c = State(tmp_path)
    assert set(c.seen) == {"s:1", "s:2"}
    assert c.notified_in(job, "favorites") and c.notified_in(other, "company_sites")
    assert set(c.runs["groups"]) == {"favorites", "company_sites"}


def test_prune(tmp_path):
    s = State(tmp_path)
    j = Job("s", "1", "PB", "https://x", "Acme")
    s.observe(j, NOW - timedelta(days=200))
    s.prune(NOW)
    assert s.seen == {}


async def _no_sleep(*_a, **_k):
    return None
