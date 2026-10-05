"""notify.destinations: chat name -> Discord channel / Telegram topic, adopted, created or fallen back."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from jobfinder.notify.destinations import Destinations
from jobfinder.sources.http import Http
from jobfinder.state import State

from .conftest import make_config

DISCORD = "https://discord.com/api/v10"
TELEGRAM = "https://api.telegram.org/bot123:abc"
DEFAULT, GUILD, CATEGORY = "555", "777", "800"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", DEFAULT)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100200")


def config(discord: dict | None = None, telegram: dict | None = None, **routing):
    routing = {"summary": "Resumen", "other": "Otros", "places": {"Lisbon": "Lisboa"}, **routing}
    return make_config(notify={"routing": routing, "discord": {"enabled": True, "bot": True, **(discord or {})},
                               "telegram": {"enabled": True, **(telegram or {})}})


def mock_guild(channels: list[dict]):
    respx.get(f"{DISCORD}/channels/{DEFAULT}").mock(
        return_value=httpx.Response(200, json={"id": DEFAULT, "guild_id": GUILD, "parent_id": CATEGORY}))
    return respx.get(f"{DISCORD}/guilds/{GUILD}/channels").mock(return_value=httpx.Response(200, json=channels))


async def resolve(cfg, state, *calls):
    created = []
    async with Http() as http:
        d = Destinations(cfg, http, state, on_created=lambda: created.append(1))
        return [await call(d) for call in calls], d, created


@respx.mock
async def test_discord_creates_a_channel_next_to_the_default_one_and_remembers_it(tmp_path):
    mock_guild([{"id": DEFAULT, "name": "ofertas", "type": 0, "parent_id": CATEGORY}])
    post = respx.post(f"{DISCORD}/guilds/{GUILD}/channels").mock(
        return_value=httpx.Response(201, json={"id": "901", "name": "lisboa", "type": 0}))
    state = State(tmp_path)
    (ids, d, created) = await resolve(config(), state, lambda d: d.discord_channel("Lisboa"),
                                      lambda d: d.discord_channel("Lisboa"))
    assert ids == ["901", "901"] and post.call_count == 1  # created once, cached for the rest of the run
    assert json.loads(post.calls[0].request.content) == {"name": "lisboa", "type": 0, "parent_id": CATEGORY}
    assert state.destination("discord", "Lisboa") == "901" and created == [1] and state.material
    # next run: the id comes from the state, Discord is not asked at all
    state.save()
    respx.reset()
    (ids, _, created) = await resolve(config(), State(tmp_path), lambda d: d.discord_channel("Lisboa"))
    assert ids == ["901"] and created == [] and not respx.calls


@respx.mock
async def test_discord_adopts_a_channel_with_the_same_name_instead_of_creating_one(tmp_path):
    mock_guild([{"id": "42", "name": "lisboa", "type": 0, "parent_id": None},
                {"id": "43", "name": "lisboa", "type": 0, "parent_id": CATEGORY},  # the one in our category wins
                {"id": "44", "name": "lisboa", "type": 2, "parent_id": CATEGORY}])  # a voice channel is not a chat
    post = respx.post(f"{DISCORD}/guilds/{GUILD}/channels")
    state = State(tmp_path)
    (ids, *_) = await resolve(config(), state, lambda d: d.discord_channel("Lisboa"))
    assert ids == ["43"] and not post.called and state.destination("discord", "Lisboa") == "43"


@respx.mock
async def test_discord_names_become_channel_names(tmp_path):
    mock_guild([])
    post = respx.post(f"{DISCORD}/guilds/{GUILD}/channels").mock(
        return_value=httpx.Response(201, json={"id": "902", "name": "x", "type": 0}))
    await resolve(config(summary="⭐ Destacadas & más"), State(tmp_path), lambda d: d.discord_channel("⭐ Destacadas & más"))
    assert json.loads(post.calls[0].request.content)["name"] == "destacadas-más"


@respx.mock
async def test_discord_explicit_ids_win_and_need_no_requests(tmp_path):
    cfg = config(discord={"channels": {"Lisboa": 123456}})
    (ids, *_) = await resolve(cfg, State(tmp_path), lambda d: d.discord_channel("Lisboa"),
                              lambda d: d.discord_channel(None))
    assert ids == ["123456", DEFAULT] and not respx.calls  # None is the default channel


@respx.mock
async def test_discord_category_by_name_is_found_or_created(tmp_path):
    mock_guild([{"id": "810", "name": "Job Finder", "type": 4}])
    post = respx.post(f"{DISCORD}/guilds/{GUILD}/channels").mock(
        return_value=httpx.Response(201, json={"id": "903", "name": "lisboa", "type": 0}))
    await resolve(config(discord={"guild_id": GUILD, "category": "job finder"}), State(tmp_path),
                  lambda d: d.discord_channel("Lisboa"))
    assert json.loads(post.calls[0].request.content)["parent_id"] == "810"
    respx.reset()
    mock_guild([])
    post = respx.post(f"{DISCORD}/guilds/{GUILD}/channels").mock(side_effect=[
        httpx.Response(201, json={"id": "811", "name": "Job Finder", "type": 4}),
        httpx.Response(201, json={"id": "904", "name": "lisboa", "type": 0})])
    await resolve(config(discord={"guild_id": GUILD, "category": "Job Finder"}), State(tmp_path / "b"),
                  lambda d: d.discord_channel("Lisboa"))
    bodies = [json.loads(c.request.content) for c in post.calls]
    assert bodies[0] == {"name": "Job Finder", "type": 4} and bodies[1]["parent_id"] == "811"


@respx.mock
async def test_discord_failure_falls_back_to_other_then_the_default_and_is_logged_once(tmp_path, capsys):
    mock_guild([{"id": "50", "name": "otros", "type": 0, "parent_id": CATEGORY}])
    respx.post(f"{DISCORD}/guilds/{GUILD}/channels").mock(
        return_value=httpx.Response(403, json={"message": "Missing Permissions", "code": 50013}))
    state = State(tmp_path)
    # "Lisboa" cannot be created: its jobs go to Otros (adopted by name); a name without any home: the default
    (ids, d, created) = await resolve(config(), state, lambda d: d.discord_channel("Lisboa"),
                                      lambda d: d.discord_channel("Resumen"),
                                      lambda d: d.discord_channel("Otros"))
    assert ids == ["50", "50", "50"] and created == [1]  # only the adopted Otros was saved
    assert state.destination("discord", "Lisboa") is None  # a failure is retried by the next run
    err = capsys.readouterr().err + capsys.readouterr().out
    assert "Missing Permissions" in err and err.count("could not find or create") == 1
    assert "Lisboa" not in err and "Resumen" not in err and GUILD not in err  # public logs: no names, no ids

    respx.reset()
    mock_guild([])
    respx.post(f"{DISCORD}/guilds/{GUILD}/channels").mock(return_value=httpx.Response(403))
    (ids, *_) = await resolve(config(), State(tmp_path / "b"), lambda d: d.discord_channel("Lisboa"))
    assert ids == [DEFAULT]  # nothing works: the default channel


@respx.mock
async def test_discord_creation_is_never_retried(tmp_path):
    mock_guild([])
    post = respx.post(f"{DISCORD}/guilds/{GUILD}/channels").mock(return_value=httpx.Response(500))
    (ids, *_) = await resolve(config(), State(tmp_path), lambda d: d.discord_channel("Lisboa"))
    # one attempt for Lisboa and one for the fallback Otros (a different channel); a retry after a timeout
    # could create a second copy of the same channel
    assert ids == [DEFAULT] and post.call_count == 2


@respx.mock
async def test_discord_needs_the_bot(tmp_path, monkeypatch):
    async with Http() as http:
        assert Destinations(config(), http, State(tmp_path)).discord_ready
        assert not Destinations(config(discord={"bot": False}), http, State(tmp_path)).discord_ready
        monkeypatch.delenv("DISCORD_CHANNEL_ID")
        assert not Destinations(config(), http, State(tmp_path)).discord_ready


@respx.mock
async def test_telegram_needs_a_group_with_topics(tmp_path):
    chat = respx.post(f"{TELEGRAM}/getChat").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"id": -100200, "is_forum": False}}))
    (ready, d, _) = await resolve(config(), State(tmp_path), lambda d: d.telegram_ready(), lambda d: d.telegram_ready())
    assert ready == [False, False] and chat.call_count == 1  # asked once per run
    respx.reset()
    respx.post(f"{TELEGRAM}/getChat").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"id": -100200, "is_forum": True}}))
    (ready, *_) = await resolve(config(), State(tmp_path), lambda d: d.telegram_ready())
    assert ready == [True]
    respx.reset()
    respx.post(f"{TELEGRAM}/getChat").mock(return_value=httpx.Response(400, json={"ok": False, "description": "chat not found"}))
    (ready, *_) = await resolve(config(), State(tmp_path), lambda d: d.telegram_ready())
    assert ready == [False]


@respx.mock
async def test_telegram_creates_a_topic_once_and_remembers_it(tmp_path):
    create = respx.post(f"{TELEGRAM}/createForumTopic").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_thread_id": 77, "name": "Lisboa"}}))
    state = State(tmp_path)
    (ids, _, created) = await resolve(config(), state, lambda d: d.telegram_topic("Lisboa"),
                                      lambda d: d.telegram_topic("Lisboa"), lambda d: d.telegram_topic(None))
    assert ids == [77, 77, None] and create.call_count == 1 and created == [1]
    assert json.loads(create.calls[0].request.content) == {"chat_id": "-100200", "name": "Lisboa"}
    assert state.destination("telegram", "Lisboa") == 77
    state.save()
    respx.reset()
    (ids, *_) = await resolve(config(), State(tmp_path), lambda d: d.telegram_topic("Lisboa"))
    assert ids == [77] and not respx.calls


@respx.mock
async def test_telegram_explicit_topics_win_and_zero_is_the_general_topic(tmp_path):
    cfg = config(telegram={"topics": {"Lisboa": 12, "Otros": 0}})
    (ids, *_) = await resolve(cfg, State(tmp_path), lambda d: d.telegram_topic("Lisboa"),
                              lambda d: d.telegram_topic("Otros"))
    assert ids == [12, None] and not respx.calls


@respx.mock
async def test_telegram_failure_falls_back_to_other_then_general_and_is_never_retried(tmp_path):
    create = respx.post(f"{TELEGRAM}/createForumTopic").mock(return_value=httpx.Response(
        400, json={"ok": False, "description": "not enough rights to create a topic"}))
    state = State(tmp_path)
    (ids, *_) = await resolve(config(telegram={"topics": {"Otros": 9}}), state, lambda d: d.telegram_topic("Lisboa"))
    assert ids == [9] and create.call_count == 1 and state.destination("telegram", "Lisboa") is None
    (ids, *_) = await resolve(config(), State(tmp_path / "b"), lambda d: d.telegram_topic("Lisboa"))
    assert ids == [None] and create.call_count == 3  # Lisboa, then Otros; the answer is General
    respx.reset()
    boom = respx.post(f"{TELEGRAM}/createForumTopic").mock(return_value=httpx.Response(500))
    await resolve(config(), State(tmp_path / "c"), lambda d: d.telegram_topic("Lisboa"))
    assert boom.call_count == 2  # one attempt each for Lisboa and Otros: no retries
