"""Discord bot mode: per-job messages with ⭐✅🗣❌ reactions and the reactions read back into the tracker."""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import quote

import httpx
import pytest
import respx

from jobfinder.app import run, tracker_sync
from jobfinder.app import test_notify as send_test_notification
from jobfinder.models import Job
from jobfinder.state import State
from jobfinder.tracker import Tracker, job_id

from .conftest import NOW
from .test_pipeline import write_config
from .test_tracker import JID, KEY, TZ_DAY, bodies, make_tz, read_csv

API = "https://discord.com/api/v10"
CHANNEL = "555"
WEBHOOK = "https://discord.com/api/webhooks/1/abc"
EMOJI = {"interested": "⭐", "applied": "✅", "interview": "🗣️", "discarded": "❌"}


@pytest.fixture(autouse=True)
def discord_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-secret")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)

    async def no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)


def discord_config(tmp_path, **kwargs) -> None:
    write_config(tmp_path, **kwargs)
    path = tmp_path / "config.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "telegram: {enabled: true}", "telegram: {enabled: false}\n  discord: {enabled: true}"), encoding="utf-8")


def mock_discord(messages: dict[str, dict] | None = None) -> dict:
    """POST returns increasing ids; GET returns ``messages[id]``; PUT (reactions) and PATCH succeed."""
    counter = iter(range(900, 1000))
    messages = messages if messages is not None else {}
    base = f"{API}/channels/{CHANNEL}/messages"

    def post(request):
        return httpx.Response(200, json={"id": str(next(counter)), "channel_id": CHANNEL})

    def get(request):
        mid = request.url.path.rsplit("/", 1)[1]
        if mid not in messages:
            return httpx.Response(404, json={"message": "Unknown Message", "code": 10008})
        return httpx.Response(200, json=messages[mid])

    return {
        "post": respx.post(base).mock(side_effect=post),
        "get": respx.get(url__regex=rf"{base}/\d+$").mock(side_effect=get),
        "react": respx.put(url__regex=rf"{base}/\d+/reactions/.+/@me$").mock(return_value=httpx.Response(204)),
        "patch": respx.patch(url__regex=rf"{base}/\d+$").mock(return_value=httpx.Response(200, json={})),
    }


def reactions(*humans: str, me: bool = True) -> list[dict]:
    """Reactions of a message: the bot reacted with all four, ``humans`` are the extra reactions of someone."""
    out = []
    for status, emoji in EMOJI.items():
        count = (1 if me else 0) + (1 if status in humans else 0)
        if count:
            out.append({"emoji": {"name": emoji}, "count": count, "me": me})
    return out


def seed(tmp_path, message_id: str = "900") -> None:
    state = State(tmp_path)
    job = Job("secret-corp", "1", "Product Manager DACH", "https://jobs.example.test/1", "Secret Corp",
              locations=["Berlin, Germany"])
    state.observe(job, NOW)
    state.mark_notified(job, "favorites", NOW)
    state.save()
    tracker = Tracker(tmp_path, "es", make_tz())
    tracker.remember_discord(JID, KEY, CHANNEL, int(message_id), TZ_DAY)
    tracker.save(NOW)


# ------------------------------------------------------------------ sending
@respx.mock
async def test_per_job_messages_get_reactions_and_are_remembered(tmp_path):
    routes = mock_discord()
    discord_config(tmp_path)
    await run(None, tmp_path, groups=["company_sites"], now=NOW)
    posts = bodies(routes["post"])
    assert posts[0] == {"content": "**💼 Webs corporativas: 1 oferta nueva**"}  # header, then one message per job
    job_post = next(p for p in posts if "embeds" in p)
    assert job_post["embeds"][0]["title"] == "Secret Corp — Product Manager DACH"
    assert all(c.request.headers["Authorization"] == "Bot bot-secret" for c in routes["post"].calls)
    urls = [str(c.request.url) for c in routes["react"].calls]
    assert len(urls) == 4 and all("/messages/901/reactions/" in u for u in urls)
    assert [u.split("/reactions/")[1].split("/@me")[0] for u in urls] == [quote(e) for e in EMOJI.values()]
    saved = json.loads((tmp_path / "state" / "tracker.json").read_text())
    assert saved["discord"]["901"]["jid"] == JID and saved["discord"]["901"]["channel"] == CHANNEL


@respx.mock
async def test_webhook_is_used_without_bot_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
    discord_config(tmp_path)
    await run(None, tmp_path, groups=["company_sites"], now=NOW)
    assert hook.call_count == 1 and "embeds" in bodies(hook)[0]
    assert not (tmp_path / "state" / "tracker.json").exists()  # no tracker without buttons


@respx.mock
async def test_missing_reaction_permission_does_not_lose_the_message(tmp_path, capsys):
    routes = mock_discord()
    routes["react"].mock(return_value=httpx.Response(403, json={"message": "Missing Permissions", "code": 50013}))
    discord_config(tmp_path)
    assert await run(None, tmp_path, groups=["company_sites"], now=NOW) == 0
    assert routes["react"].call_count == 1  # reported once, not four times
    assert "Add Reactions" in capsys.readouterr().err


@respx.mock
async def test_bot_error_reason_reaches_the_log(tmp_path, capsys):
    respx.post(f"{API}/channels/{CHANNEL}/messages").mock(
        return_value=httpx.Response(403, json={"message": "Missing Access", "code": 50001}))
    discord_config(tmp_path)
    assert await run(None, tmp_path, groups=["company_sites"], now=NOW) == 1
    err = capsys.readouterr().err
    assert "Missing Access" in err and "bot-secret" not in err


@respx.mock
async def test_test_message_has_reactions(tmp_path):
    routes = mock_discord()
    discord_config(tmp_path)
    assert await send_test_notification(None, tmp_path) == 0
    assert "Reacciones de prueba" in bodies(routes["post"])[0]["content"]
    assert routes["react"].call_count == 4


# ------------------------------------------------------------------ syncing
@respx.mock
async def test_reaction_saves_the_row_and_marks_the_message(tmp_path):
    discord_config(tmp_path)
    seed(tmp_path)
    routes = mock_discord({"900": {"id": "900", "content": "", "reactions": reactions("applied")}})
    assert await tracker_sync(None, tmp_path, push=False, now=NOW) == 0
    assert read_csv(tmp_path) == [{"id": JID, "estado": "aplicado", "fecha": TZ_DAY, "empresa": "Secret Corp",
                                   "puesto": "Product Manager DACH", "ubicacion": "Berlin, Germany",
                                   "url": "https://jobs.example.test/1", "notas": "", "historial": f"{TZ_DAY} aplicado"}]
    assert bodies(routes["patch"]) == [{"content": "» ✅ Aplicado «"}]

    # the next sync sees the same reaction: nothing changes (idempotent), the marker is already there
    routes["get"].mock(return_value=httpx.Response(200, json={
        "id": "900", "content": "» ✅ Aplicado «", "reactions": reactions("applied")}))
    await tracker_sync(None, tmp_path, push=False, now=NOW + timedelta(hours=2))
    assert routes["patch"].call_count == 1
    assert read_csv(tmp_path)[0]["historial"] == f"{TZ_DAY} aplicado"

    # a new, different reaction moves it on and keeps the history; the marker line is replaced
    routes["get"].mock(return_value=httpx.Response(200, json={
        "id": "900", "content": "» ✅ Aplicado «", "reactions": reactions("applied", "interview")}))
    await tracker_sync(None, tmp_path, push=False, now=NOW + timedelta(days=3))
    row = read_csv(tmp_path)[0]
    assert row["estado"] == "entrevista" and row["historial"] == f"{TZ_DAY} aplicado; 2026-09-30 entrevista"
    assert bodies(routes["patch"])[-1] == {"content": "» 🗣 Entrevista «"}


@respx.mock
async def test_the_bots_own_reactions_are_not_feedback(tmp_path):
    discord_config(tmp_path)
    seed(tmp_path)
    routes = mock_discord({"900": {"id": "900", "content": "", "reactions": reactions()}})
    await tracker_sync(None, tmp_path, push=False, now=NOW)
    assert not (tmp_path / "tracker" / "applications.csv").exists() and routes["patch"].call_count == 0


@respx.mock
async def test_removing_and_adding_a_reaction_again_counts_again(tmp_path):
    discord_config(tmp_path)
    seed(tmp_path)
    routes = mock_discord({"900": {"id": "900", "content": "", "reactions": reactions("discarded")}})
    await tracker_sync(None, tmp_path, push=False, now=NOW)
    assert read_csv(tmp_path)[0]["estado"] == "descartado"
    routes["get"].mock(return_value=httpx.Response(200, json={"id": "900", "content": "", "reactions": reactions()}))
    await tracker_sync(None, tmp_path, push=False, now=NOW + timedelta(hours=1))  # reaction removed
    routes["get"].mock(return_value=httpx.Response(200, json={
        "id": "900", "content": "", "reactions": reactions("interested")}))
    await tracker_sync(None, tmp_path, push=False, now=NOW + timedelta(hours=2))
    assert read_csv(tmp_path)[0]["estado"] == "interesa"


@respx.mock
async def test_deleted_messages_are_forgotten_and_old_ones_not_polled(tmp_path):
    discord_config(tmp_path)
    seed(tmp_path, "900")
    tracker = Tracker(tmp_path, "es", make_tz())
    tracker.remember_discord("old", None, CHANNEL, 800, "2026-06-01")  # older than the 60-day window
    tracker.save(NOW)
    routes = mock_discord()  # 900 is gone (404)
    await tracker_sync(None, tmp_path, push=False, now=NOW)
    assert [c.request.url.path.rsplit("/", 1)[1] for c in routes["get"].calls] == ["900"]
    assert "discord" not in json.loads((tmp_path / "state" / "tracker.json").read_text())


@respx.mock
async def test_lost_access_fails_the_sync_with_a_hint(tmp_path, capsys):
    discord_config(tmp_path)
    seed(tmp_path)
    respx.get(url__regex=rf"{API}/channels/{CHANNEL}/messages/\d+$").mock(
        return_value=httpx.Response(403, json={"message": "Missing Access"}))
    assert await tracker_sync(None, tmp_path, push=False, now=NOW) == 1
    assert "Read Message History" in capsys.readouterr().err


@respx.mock
async def test_unknown_job_uses_the_embed(tmp_path):
    discord_config(tmp_path)
    tracker = Tracker(tmp_path, "es", make_tz())
    tracker.remember_discord(job_id("gone:9"), "gone:9", CHANNEL, 900, TZ_DAY)
    tracker.save(NOW)
    mock_discord({"900": {"id": "900", "content": "", "reactions": reactions("interested"), "embeds": [
        {"title": "Other Corp — Fallback Title", "url": "https://jobs.example.test/fallback"}]}})
    await tracker_sync(None, tmp_path, push=False, now=NOW)
    row = read_csv(tmp_path)[0]
    assert (row["empresa"], row["puesto"], row["url"]) == ("Other Corp", "Fallback Title",
                                                          "https://jobs.example.test/fallback")


@respx.mock
async def test_test_message_reaction_is_not_saved(tmp_path):
    discord_config(tmp_path)
    tracker = Tracker(tmp_path, "es", make_tz())
    tracker.remember_discord("test", "", CHANNEL, 900, TZ_DAY)
    tracker.save(NOW)
    routes = mock_discord({"900": {"id": "900", "content": "🔔 Prueba", "reactions": reactions("interview")}})
    await tracker_sync(None, tmp_path, push=False, now=NOW)
    assert not (tmp_path / "tracker" / "applications.csv").exists()
    assert bodies(routes["patch"]) == [{"content": "» 🗣 Entrevista «\n🔔 Prueba"}]
