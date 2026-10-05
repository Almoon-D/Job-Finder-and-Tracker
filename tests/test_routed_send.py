"""Routed sends: the Discord bot and the Telegram topics, with one offer in several chats."""

from __future__ import annotations

import json
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from jobfinder.models import Job
from jobfinder.notify.base import ChannelError, Notification
from jobfinder.notify.channels import Discord, Telegram
from jobfinder.notify.destinations import Destinations
from jobfinder.sources.http import Http
from jobfinder.state import State
from jobfinder.tracker.buttons import job_id

from .conftest import NOW, make_config

TZ = ZoneInfo("Europe/Madrid")
DISCORD = "https://discord.com/api/v10"
TELEGRAM = "https://api.telegram.org/bot123:abc"
HOOK = "https://discord.example.test/hook"
CHANNELS = {"Resumen": 11, "Destacadas": 12, "Lisboa": 13, "Otros": 14}
TOPICS = {"Resumen": 0, "Destacadas": 6, "Lisboa": 7, "Otros": 8}  # 0 = the General topic
ROUTING = {"summary": "Resumen", "highlights": "Destacadas", "other": "Otros", "places": {"Lisbon": "Lisboa"}}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "555")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100200")


def config(bot: bool = True):
    return make_config(notify={
        "routing": ROUTING,
        "discord": {"enabled": True, "bot": bot, "channels": CHANNELS},
        "telegram": {"enabled": True, "topics": TOPICS},
    })


def job(n: int, place: str | None, score: int = 70) -> Job:
    j = Job("s", str(n), f"Role {n}", f"https://jobs.example.test/{n}", f"Company {n}", [place or ""])
    j.score, j.extra["target"] = score, place
    return j


def notification(jobs=None, **kw) -> Notification:
    jobs = [job(1, "Lisbon", 90), job(2, None, 60)] if jobs is None else jobs
    return Notification("company_sites", "Webs", jobs, "es", TZ, NOW,
                        **{"format": "per_job", "buttons": True, "sources_ok": 3, "sources_total": 3, **kw})


def bodies(route) -> list[dict]:
    return [json.loads(c.request.content) for c in route.calls]


# ------------------------------------------------------------------- Discord
def mock_discord(failing: dict[int, list[int]] | None = None) -> dict[int, respx.Route]:
    """One route per channel; ``failing[channel]`` lists the statuses of its first answers (then 200)."""
    ids = iter(range(9000000000000000001, 9000000000000000900))
    failing = {k: list(v) for k, v in (failing or {}).items()}
    routes = {}
    for channel in CHANNELS.values():
        def answer(request, channel=channel):
            if failing.get(channel):
                status = failing[channel].pop(0)
                if status != 200:
                    return httpx.Response(status, json={"message": "Missing Access", "code": 50001})
            return httpx.Response(200, json={"id": str(next(ids))})

        routes[channel] = respx.post(f"{DISCORD}/channels/{channel}/messages").mock(side_effect=answer)
    respx.put(url__regex=rf"{DISCORD}/channels/\d+/messages/\d+/reactions/.+/@me").mock(
        return_value=httpx.Response(204))
    return routes


async def discord_send(cfg, n, tmp_path, destinations: bool = True):
    async with Http() as http:
        dest = Destinations(cfg, http, State(tmp_path)) if destinations else None
        await Discord(cfg, http, dest).send(n)


@respx.mock
async def test_discord_the_same_offer_goes_to_its_place_and_to_the_highlights(tmp_path):
    routes = mock_discord()
    n = notification()
    await discord_send(config(), n, tmp_path)

    # Resumen: one silent compact index of everything, no reactions
    (index,) = bodies(routes[11])
    assert index["flags"] == 4096 and "Role 1" in json.dumps(index) and "Role 2" in json.dumps(index)
    assert "https://jobs.example.test/1" in json.dumps(index)
    # each chat gets its own cards: header + one per offer (the offer found by the AI is in two chats)
    cards = {ch: [b for b in bodies(routes[ch]) if "embeds" in b] for ch in (12, 13, 14)}
    assert {ch: [b["embeds"][0]["url"].rsplit("/", 1)[1] for b in c] for ch, c in cards.items()} == {
        12: ["1"], 13: ["1"], 14: ["2"]}
    assert all("flags" not in b for ch in (12, 13, 14) for b in bodies(routes[ch]))
    # the bot knows it is the same offer: both copies carry the same tracker id, in different channels
    refs = {(jid, chat) for jid, _, chat, _ in n.sent}
    assert refs == {(job_id("s:1"), "discord:12"), (job_id("s:1"), "discord:13"), (job_id("s:2"), "discord:14")}
    assert len({mid for _, _, _, mid in n.sent}) == 3


@respx.mock
async def test_discord_a_chat_without_a_footer_sends_no_empty_footer(tmp_path):
    routes = mock_discord()
    n = notification(format="digest", buttons=False)  # embeds, not cards: the health line lives in the index only
    await discord_send(config(), n, tmp_path)
    for channel in (12, 13, 14):
        for body in bodies(routes[channel]):
            assert all("footer" not in e for e in body["embeds"]), body  # Discord rejects `footer: {text: ""}`
    assert bodies(routes[11])[0]["embeds"][-1]["footer"]["text"]  # the index keeps its sources line


@respx.mock
async def test_discord_a_chat_that_refuses_its_first_message_falls_back_to_the_catch_all(tmp_path, capsys):
    routes = mock_discord(failing={13: [403, 403, 403]})  # Lisboa: no access
    await discord_send(config(), notification(), tmp_path)  # does not raise
    others = [b for b in bodies(routes[14]) if "embeds" in b]
    assert sorted(b["embeds"][0]["url"].rsplit("/", 1)[1] for b in others) == ["1", "2"]  # Lisboa's offer landed here
    assert "catch-all" in capsys.readouterr().err


@respx.mock
async def test_discord_a_chat_that_fails_midway_is_not_repeated_elsewhere(tmp_path, capsys):
    routes = mock_discord(failing={13: [200, 403, 403, 403]})  # the header goes out, the card does not
    await discord_send(config(), notification(), tmp_path)  # the other chats succeeded: no error
    assert [b["embeds"][0]["url"].rsplit("/", 1)[1] for b in bodies(routes[14]) if "embeds" in b] == ["2"]
    assert "1 of 4 chats failed" in capsys.readouterr().err


@respx.mock
async def test_discord_fails_only_when_every_chat_failed(tmp_path):
    mock_discord(failing={ch: [500] * 99 for ch in CHANNELS.values()})
    with pytest.raises(ChannelError, match="HTTP 500"):
        await discord_send(config(), notification(), tmp_path)


@respx.mock
async def test_discord_a_test_reaches_every_chat_and_fails_if_one_is_broken(tmp_path):
    routes = mock_discord()
    await discord_send(config(), notification([], test=True, buttons=False), tmp_path)
    assert {ch: len(r.calls) for ch, r in routes.items()} == {11: 1, 12: 1, 13: 1, 14: 1}
    assert all("content" in bodies(r)[0] for r in routes.values())
    respx.reset()
    mock_discord(failing={13: [403]})
    with pytest.raises(ChannelError, match="HTTP 403"):  # a test is for finding exactly this
        await discord_send(config(), notification([], test=True, buttons=False), tmp_path)


@respx.mock
async def test_discord_routed_reports_and_empty_notices_go_through_the_bot_to_the_overview(tmp_path):
    from jobfinder.notify.base import Report

    routes = mock_discord()
    report = notification([], report=Report("Resumen semanal", [("Cifras", ["3 ofertas"])]))
    await discord_send(config(), report, tmp_path)
    assert [len(r.calls) for r in routes.values()] == [1, 0, 0, 0]
    assert "Cifras" in json.dumps(bodies(routes[11])[0])


@respx.mock
async def test_discord_without_destinations_or_without_the_bot_nothing_is_routed(tmp_path):
    webhook = respx.post(HOOK).mock(return_value=httpx.Response(204))
    bot = mock_discord()
    n = notification(buttons=False)
    await discord_send(config(bot=False), n, tmp_path)  # webhook only: it cannot choose a channel
    await discord_send(config(), n, tmp_path, destinations=False)  # a caller that passes no destinations
    assert webhook.call_count == 2 and not any(r.called for r in bot.values())


# ------------------------------------------------------------------ Telegram
def mock_telegram(forum: bool = True, refuse_topic: int | None = None):
    ids = iter(range(1000, 2000))
    respx.post(f"{TELEGRAM}/getChat").mock(return_value=httpx.Response(
        200, json={"ok": True, "result": {"id": -100200, "is_forum": forum}}))

    def answer(request):
        if refuse_topic is not None and json.loads(request.content).get("message_thread_id") == refuse_topic:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: message thread not found"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": next(ids), "chat": {"id": -100200}}})

    return respx.post(f"{TELEGRAM}/sendMessage").mock(side_effect=answer)


async def telegram_send(cfg, n, tmp_path):
    async with Http() as http:
        await Telegram(cfg, http, Destinations(cfg, http, State(tmp_path))).send(n)


def sent_to(route) -> dict[int | None, list[dict]]:
    out: dict[int | None, list[dict]] = {}
    for body in bodies(route):
        out.setdefault(body.get("message_thread_id"), []).append(body)
    return out


@respx.mock
async def test_telegram_the_same_offer_goes_to_its_topic_and_to_the_highlights(tmp_path):
    send = mock_telegram()
    n = notification()
    await telegram_send(config(), n, tmp_path)
    topics = sent_to(send)
    # General (id 0) has no thread id: the silent, button-free index
    (index,) = topics[None]
    assert index["disable_notification"] is True and "reply_markup" not in index
    assert "Role 1" in index["text"] and "Role 2" in index["text"]
    # every other topic: header + cards with the tracker buttons
    assert {t: len(m) for t, m in topics.items()} == {None: 1, 6: 2, 7: 2, 8: 2}
    assert all("reply_markup" in m for t in (6, 7, 8) for m in topics[t][1:])
    assert "Role 1" in topics[6][1]["text"] and "Role 1" in topics[7][1]["text"] and "Role 2" in topics[8][1]["text"]
    # one offer, one tracker id, two messages (the group is one chat; the topics only differ by message id)
    copies = [(jid, chat, mid) for jid, _, chat, mid in n.sent if jid == job_id("s:1")]
    assert len(copies) == 2 and {c[1] for c in copies} == {"-100200"} and len({c[2] for c in copies}) == 2


@respx.mock
async def test_telegram_a_group_without_topics_gets_the_one_chat_behaviour(tmp_path, capsys):
    send = mock_telegram(forum=False)
    await telegram_send(config(), notification(), tmp_path)
    assert len(send.calls) == 3 and all("message_thread_id" not in b for b in bodies(send))  # header + 2 cards
    assert "Topics" in capsys.readouterr().err


@respx.mock
async def test_telegram_a_deleted_topic_falls_back_to_the_catch_all_one(tmp_path, capsys):
    send = mock_telegram(refuse_topic=7)  # Lisboa's topic was deleted
    await telegram_send(config(), notification(), tmp_path)
    topics = sent_to(send)
    ok = [b for b in topics[8] if "Role" in b["text"]]
    assert sorted("Role 1" in b["text"] for b in ok) == [False, True]  # Otros received Lisboa's offer too
    assert "catch-all" in capsys.readouterr().err


@respx.mock
async def test_telegram_a_test_reaches_every_topic(tmp_path):
    send = mock_telegram()
    await telegram_send(config(), notification([], test=True), tmp_path)
    assert sorted(sent_to(send), key=lambda t: t or 0) == [None, 6, 7, 8]
    assert "reply_markup" not in sent_to(send)[None][0]  # the overview has nothing to press
    respx.reset()
    mock_telegram(refuse_topic=7)
    with pytest.raises(ChannelError, match="HTTP 400"):
        await telegram_send(config(), notification([], test=True), tmp_path)


# ------------------------------------------------------------ whole runs
@respx.mock
async def test_a_run_creates_the_chats_routes_the_offers_and_logs_nothing_private(tmp_path, monkeypatch, capsys):
    from datetime import timedelta

    from jobfinder.app import run

    from .test_pipeline import write_config

    monkeypatch.setenv("GITHUB_ACTIONS", "true")  # public CI logs: counts only
    write_config(tmp_path, favorite=True)
    cfg = (tmp_path / "config.yaml").read_text().replace(
        "notify:\n  telegram: {enabled: true}",
        "notify:\n  discord: {enabled: true, bot: true}\n  routing: {summary: Resumen, highlights: Destacadas, "
        "other: Otros, places: {Germany: Alemania, Lisbon: Lisboa}}")
    (tmp_path / "config.yaml").write_text(cfg)

    respx.get(f"{DISCORD}/channels/555").mock(
        return_value=httpx.Response(200, json={"id": "555", "guild_id": "777", "parent_id": "800"}))
    respx.get(f"{DISCORD}/guilds/777/channels").mock(return_value=httpx.Response(200, json=[]))
    made: dict[str, str] = {}

    def create(request):
        body = json.loads(request.content)
        made[str(9100 + len(made))] = body["name"]
        return httpx.Response(201, json={"id": str(9100 + len(made) - 1), "name": body["name"], "type": 0})

    creating = respx.post(f"{DISCORD}/guilds/777/channels").mock(side_effect=create)
    ids = iter(range(9000000000000000001, 9000000000000000900))
    posts = respx.post(url__regex=rf"{DISCORD}/channels/\d+/messages").mock(
        side_effect=lambda request: httpx.Response(200, json={"id": str(next(ids))}))
    respx.put(url__regex=rf"{DISCORD}/channels/\d+/messages/\d+/reactions/.+/@me").mock(
        return_value=httpx.Response(204))
    history = respx.get(url__regex=rf"{DISCORD}/channels/\d+/messages").mock(return_value=httpx.Response(200, json=[]))

    def by_name() -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for call in posts.calls:
            channel = call.request.url.path.split("/")[-2]
            out.setdefault(made[channel], []).append(call.request.content.decode())
        return out

    assert await run(None, tmp_path, groups=["company_sites"], now=NOW) == 0
    assert sorted(made.values()) == ["alemania", "destacadas", "resumen"]  # only the chats that had something
    sent = by_name()
    assert all("Product Manager DACH" in "".join(sent[name]) for name in made.values())  # the favourite, thrice
    assert '"flags": 4096' in sent["resumen"][0] or '"flags":4096' in sent["resumen"][0]

    saved = json.loads((tmp_path / "state" / "runs.json").read_text())["destinations"]["discord"]
    assert sorted(saved) == ["Alemania", "Destacadas", "Resumen"] and sorted(saved.values()) == sorted(made)

    # the evening run: nothing new, and the notice goes to the overview only; no chat is created twice
    before = {k: len(v) for k, v in by_name().items()}
    assert await run(None, tmp_path, groups=["company_sites"], now=NOW + timedelta(hours=11, minutes=5)) == 0
    after = {k: len(v) for k, v in by_name().items()}
    assert creating.call_count == 3
    # only the chats that hold offers with ✅ ❌ are read (the index has none), each once
    assert sorted(made[c.request.url.path.split("/")[-2]] for c in history.calls) == ["alemania", "destacadas"]
    assert {k for k in after if after[k] != before.get(k)} == {"resumen"}
    assert "Sin novedades" in by_name()["resumen"][-1]

    logs = capsys.readouterr()
    text = logs.out + logs.err
    for private in ("Resumen", "Destacadas", "Alemania", "Lisboa", "resumen", "alemania", "Secret Corp",
                    "Product Manager", "9100", "9000000000", "777", "bot-token"):
        assert private not in text, private


@respx.mock
async def test_test_notify_creates_the_chats_saves_them_and_lists_routing_by_counts(tmp_path, monkeypatch, capsys):
    from jobfinder.app import test_notify as send_test_notification

    from .test_pipeline import write_config

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    write_config(tmp_path)
    cfg = (tmp_path / "config.yaml").read_text().replace(
        "notify:\n  telegram: {enabled: true}",
        "notify:\n  discord: {enabled: true, bot: true, channels: {Otros: 14}}\n"
        "  telegram: {enabled: true, topics: {Otros: 8}}\n"
        "  routing: {summary: Resumen, other: Otros, places: {Germany: Alemania}}")
    (tmp_path / "config.yaml").write_text(cfg)
    respx.get(f"{DISCORD}/channels/555").mock(
        return_value=httpx.Response(200, json={"id": "555", "guild_id": "777", "parent_id": None}))
    respx.get(f"{DISCORD}/guilds/777/channels").mock(return_value=httpx.Response(200, json=[]))
    ids = iter(range(9100, 9200))
    respx.post(f"{DISCORD}/guilds/777/channels").mock(
        side_effect=lambda request: httpx.Response(201, json={"id": str(next(ids)), "type": 0}))
    discord = respx.post(url__regex=rf"{DISCORD}/channels/\d+/messages").mock(
        return_value=httpx.Response(200, json={"id": "1"}))
    respx.post(f"{TELEGRAM}/getChat").mock(return_value=httpx.Response(
        200, json={"ok": True, "result": {"id": -100200, "is_forum": True}}))
    topics = iter(range(70, 80))
    respx.post(f"{TELEGRAM}/createForumTopic").mock(side_effect=lambda request: httpx.Response(
        200, json={"ok": True, "result": {"message_thread_id": next(topics)}}))
    telegram = respx.post(f"{TELEGRAM}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))

    assert await send_test_notification(None, tmp_path, push=False) == 0
    assert len(discord.calls) == 3 and len(telegram.calls) == 3  # Resumen, Alemania, Otros: a message in each
    assert sorted(b["message_thread_id"] for b in bodies(telegram)) == [8, 70, 71]  # Otros is written; two created
    saved = json.loads((tmp_path / "state" / "runs.json").read_text())["destinations"]
    assert sorted(saved["discord"]) == ["Alemania", "Resumen"] and sorted(saved["telegram"]) == ["Alemania", "Resumen"]
    text = (lambda c: c.out + c.err)(capsys.readouterr())
    assert "routing: 3 chats (ids in the config: 1 Discord channel(s), 1 Telegram topic(s)" in text
    assert "test notification: 2/2 channels OK" in text
    for private in ("Resumen", "Alemania", "Otros", "9100", "777"):
        assert private not in text, private
