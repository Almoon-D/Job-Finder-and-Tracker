"""Tracker: Telegram buttons and commands (simulated getUpdates), the hand-editable CSV and merges."""

from __future__ import annotations

import csv
import json
import subprocess
from datetime import timedelta

import httpx
import pytest
import respx

from jobfinder.app import run, tracker_sync
from jobfinder.app import test_notify as send_test_notification
from jobfinder.models import Job
from jobfinder.state import State
from jobfinder.storage.datarepo import commit_and_push
from jobfinder.tracker import BUTTONS, Tracker, TrackerOp, callback_data, job_id, keyboard, parse_callback
from jobfinder.tracker.store import parse_status

from .conftest import NOW
from .test_datarepo import git, setup_repos
from .test_pipeline import write_config

TOKEN, CHAT = "123:abc", "42"
BASE = f"https://api.telegram.org/bot{TOKEN}"
KEY = "secret-corp:1"
JID = job_id(KEY)
TZ_DAY = "2026-09-27"


@pytest.fixture(autouse=True)
def telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)

    async def no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)


def button(update_id: int, data: str, chat: int = 42, message_id: int = 7) -> dict:
    return {"update_id": update_id, "callback_query": {
        "id": f"q{update_id}", "data": data, "from": {"id": 1},
        "message": {"message_id": message_id, "chat": {"id": chat}, "text": "Other Corp — Fallback Title\n📍 Lisbon",
                    "entities": [{"type": "text_link", "offset": 0, "length": 5,
                                  "url": "https://jobs.example.test/fallback"}]}}}


def command(update_id: int, text: str, chat: int = 42) -> dict:
    return {"update_id": update_id, "message": {"message_id": 100 + update_id, "chat": {"id": chat}, "text": text}}


def mock_telegram(*pages: list[dict], updates_status: int = 200, answer_status: int = 400) -> dict:
    """getUpdates returns the pages in order, then nothing. answerCallbackQuery fails like a late answer."""
    queue = list(pages)

    def updates(request):
        if updates_status != 200:
            return httpx.Response(updates_status, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": queue.pop(0) if queue else []})

    return {
        "updates": respx.post(f"{BASE}/getUpdates").mock(side_effect=updates),
        "answer": respx.post(f"{BASE}/answerCallbackQuery").mock(return_value=httpx.Response(
            answer_status, json={"ok": False, "description": "Bad Request: query is too old"})),
        "edit": respx.post(f"{BASE}/editMessageReplyMarkup").mock(return_value=httpx.Response(200, json={"ok": True})),
        "send": respx.post(f"{BASE}/sendMessage").mock(return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 500, "chat": {"id": 42}}})),
    }


def seed_state(tmp_path) -> None:
    """A job that was notified with buttons in two messages (favourite alert and digest)."""
    state = State(tmp_path)
    job = Job("secret-corp", "1", "Product Manager DACH", "https://jobs.example.test/1", "Secret Corp",
              locations=["Berlin, Germany"])
    state.observe(job, NOW)
    state.mark_notified(job, "favorites", NOW)
    state.save()
    tracker = Tracker(tmp_path, "es", make_tz())
    tracker.remember(JID, KEY, CHAT, 7, TZ_DAY)
    tracker.remember(JID, KEY, CHAT, 9, TZ_DAY)
    tracker.save(NOW)


def make_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo("Europe/Berlin")


def read_csv(tmp_path) -> list[dict]:
    with (tmp_path / "tracker" / "applications.csv").open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def bodies(route) -> list[dict]:
    return [json.loads(c.request.content) for c in route.calls]


# ------------------------------------------------------------------- buttons
def test_callback_data_is_compact_and_round_trips():
    jid = job_id("linkedin-busquedas:" + "9" * 200)
    for status in BUTTONS:
        data = callback_data(status, jid)
        assert len(data.encode()) <= 64
        assert parse_callback(data) == (status, jid)
    assert parse_callback("jf1:offer:abc") is None  # not a button
    assert parse_callback("something else") is None
    kb = keyboard(jid, "applied", "es")["inline_keyboard"]
    labels = [b["text"] for row in kb for b in row]
    assert [len(row) for row in kb] == [2, 2]
    assert labels == ["⭐ Interesa", "» ✅ Aplicado «", "🗣 Entrevista", "❌ Descartar"]


def test_status_parsing_accepts_both_languages():
    assert parse_status("✅ Aplicada") == "applied"
    assert parse_status("ENTREVISTA") == "interview"
    assert parse_status("discarded") == "discarded"
    assert parse_status("llamada pendiente") is None


# -------------------------------------------------------------------- sync
@respx.mock
async def test_button_press_saves_row_and_updates_every_keyboard(tmp_path):
    write_config(tmp_path)
    seed_state(tmp_path)
    routes = mock_telegram([button(10, callback_data("applied", JID))])
    assert await tracker_sync(None, tmp_path, push=False, now=NOW) == 0

    rows = read_csv(tmp_path)
    header = (tmp_path / "tracker" / "applications.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header == "id,estado,fecha,empresa,puesto,ubicacion,url,notas,historial"
    assert rows == [{"id": JID, "estado": "aplicado", "fecha": TZ_DAY, "empresa": "Secret Corp",
                     "puesto": "Product Manager DACH", "ubicacion": "Berlin, Germany",
                     "url": "https://jobs.example.test/1", "notas": "", "historial": f"{TZ_DAY} aplicado"}]
    # the late answer (HTTP 400) is ignored; both messages with this job's buttons are updated
    assert routes["answer"].call_count == 1
    edits = bodies(routes["edit"])
    assert sorted(e["message_id"] for e in edits) == [7, 9]
    assert all(e["reply_markup"]["inline_keyboard"][0][1]["text"] == "» ✅ Aplicado «" for e in edits)
    saved = json.loads((tmp_path / "state" / "tracker.json").read_text())
    assert saved["offset"] == 11
    feed = json.loads((tmp_path / "feeds" / "tracker.json").read_text())
    assert feed["counts"]["applied"] == 1 and feed["items"][0]["status_label"] == "✅ Aplicado"

    # next sync confirms the processed updates with the saved offset; pressing it again changes nothing
    routes["updates"].side_effect = None
    routes["updates"].return_value = httpx.Response(200, json={"ok": True, "result": [
        button(11, callback_data("applied", JID))]})
    await tracker_sync(None, tmp_path, push=False, now=NOW + timedelta(hours=2))
    assert json.loads(routes["updates"].calls[-1].request.content)["offset"] == 11
    assert read_csv(tmp_path)[0]["historial"] == f"{TZ_DAY} aplicado"

    # a later status keeps the history
    routes["updates"].return_value = httpx.Response(200, json={"ok": True, "result": [
        button(12, callback_data("interview", JID))]})
    await tracker_sync(None, tmp_path, push=False, now=NOW + timedelta(days=3))
    row = read_csv(tmp_path)[0]
    assert row["estado"] == "entrevista" and row["historial"] == f"{TZ_DAY} aplicado; 2026-09-30 entrevista"


@respx.mock
async def test_unknown_job_uses_the_message_itself(tmp_path):
    write_config(tmp_path)
    mock_telegram([button(1, callback_data("interested", "0123456789ab"))])
    await tracker_sync(None, tmp_path, push=False, now=NOW)
    row = read_csv(tmp_path)[0]
    assert (row["empresa"], row["puesto"], row["url"]) == ("Other Corp", "Fallback Title",
                                                          "https://jobs.example.test/fallback")


@respx.mock
async def test_other_chats_and_foreign_buttons_are_ignored(tmp_path):
    write_config(tmp_path)
    seed_state(tmp_path)
    routes = mock_telegram([button(1, callback_data("applied", JID), chat=99), button(2, "other-bot:data"),
                            command(3, "/estado", chat=99), command(4, "hola")])
    await tracker_sync(None, tmp_path, push=False, now=NOW)
    assert not (tmp_path / "tracker" / "applications.csv").exists()
    assert routes["edit"].call_count == 0 and routes["send"].call_count == 0
    assert json.loads((tmp_path / "state" / "tracker.json").read_text())["offset"] == 5  # still confirmed


@respx.mock
async def test_test_message_buttons_are_not_saved(tmp_path):
    write_config(tmp_path)
    routes = mock_telegram([button(1, callback_data("interview", "test"))])
    await tracker_sync(None, tmp_path, push=False, now=NOW)
    assert not (tmp_path / "tracker" / "applications.csv").exists()
    edit = bodies(routes["edit"])[0]
    assert edit["message_id"] == 7 and edit["reply_markup"]["inline_keyboard"][1][0]["text"] == "» 🗣 Entrevista «"


@respx.mock
async def test_status_and_pending_commands(tmp_path):
    write_config(tmp_path)
    (tmp_path / "tracker").mkdir()
    (tmp_path / "tracker" / "applications.csv").write_text(
        "id,estado,fecha,empresa,puesto,ubicacion,url,notas,historial\n"
        "a1,interesa,2026-09-25,Acme,Analyst,,https://x.test/1,,2026-09-25 interesa\n"
        "a2,aplicado,2026-09-01,Beta,Associate <Senior>,,https://x.test/2,,2026-09-01 aplicado\n"
        "a3,aplicado,2026-09-26,Gamma,Banker,,,,2026-09-26 aplicado\n"
        "a4,descartado,2026-09-20,Delta,Sales,,,,2026-09-20 descartado\n", encoding="utf-8")
    routes = mock_telegram([command(1, "/estado"), command(2, "/pendientes@MiBot"), command(3, "/ayuda")])
    await tracker_sync(None, tmp_path, push=False, now=NOW)
    status, pending, help_text = [b["text"] for b in bodies(routes["send"])]
    assert "⭐ Interesa 1 · ✅ Aplicado 2 · ❌ Descartado 1" in status
    assert "Gamma — Banker (26/09: ✅ Aplicado)" in status
    assert "Por aplicar (1)" in pending and "Acme — Analyst" in pending
    assert "sin novedades desde hace 14+ días (1)" in pending and "Associate &lt;Senior&gt;" in pending
    assert "Gamma" not in pending  # applied yesterday: not a follow-up yet
    assert "/pendientes" in help_text


@respx.mock
async def test_webhook_conflict_removes_the_webhook(tmp_path, capsys):
    write_config(tmp_path)
    mock_telegram(updates_status=409)
    info = respx.post(f"{BASE}/getWebhookInfo").mock(return_value=httpx.Response(
        200, json={"ok": True, "result": {"url": "https://hooks.example.test/tg"}}))
    delete = respx.post(f"{BASE}/deleteWebhook").mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await tracker_sync(None, tmp_path, push=False, now=NOW) == 0  # not fatal
    assert info.call_count == 1 and delete.call_count == 1
    err = capsys.readouterr().err
    assert "webhook was set" in err and "hooks.example.test" not in err  # the url is never logged


@respx.mock
async def test_conflict_without_a_webhook_is_a_concurrent_run(tmp_path, capsys):
    write_config(tmp_path)
    mock_telegram(updates_status=409)
    respx.post(f"{BASE}/getWebhookInfo").mock(return_value=httpx.Response(200, json={"ok": True, "result": {"url": ""}}))
    delete = respx.post(f"{BASE}/deleteWebhook").mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await tracker_sync(None, tmp_path, push=False, now=NOW) == 0
    assert delete.call_count == 0 and "another run" in capsys.readouterr().err
    mock_telegram(updates_status=502)
    assert await tracker_sync(None, tmp_path, push=False, now=NOW) == 1


# ------------------------------------------------------------- diagnostics
@respx.mock
async def test_telegram_error_reason_is_logged_without_ids(tmp_path, capsys):
    write_config(tmp_path)
    respx.post(f"{BASE}/sendMessage").mock(return_value=httpx.Response(
        400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}))
    assert await send_test_notification(None, tmp_path) == 1
    err = capsys.readouterr().err
    assert "notify/telegram: failed (HTTP 400: Bad Request: chat not found)" in err
    assert TOKEN not in err


async def test_test_notify_lists_what_is_missing(tmp_path, monkeypatch, capsys):
    write_config(tmp_path)
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert await send_test_notification(None, tmp_path) == 1
    err = capsys.readouterr().err
    assert "telegram: enabled (TELEGRAM_BOT_TOKEN=set, TELEGRAM_CHAT_ID=MISSING)" in err
    assert "discord: disabled in config.yaml" in err
    assert "notify/telegram: failed (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID)" in err
    assert TOKEN not in err


async def test_no_enabled_channel_is_called_out(tmp_path, capsys):
    write_config(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("telegram: {enabled: true}", "telegram: {enabled: false}"),
                    encoding="utf-8")
    assert await send_test_notification(None, tmp_path) == 1
    assert "no channel is enabled" in capsys.readouterr().err


# --------------------------------------------------------------------- CSV
def test_manual_edits_are_kept_and_recorded(tmp_path):
    path = tmp_path / "tracker" / "applications.csv"
    path.parent.mkdir()
    path.write_text(
        "id,estado,fecha,empresa,puesto,ubicacion,url,notas,historial,salario\n"
        "a1,Entrevista,2026-09-26,Acme,Analyst,,,\"llamar el lunes, 10:00\",2026-09-20 aplicado,90k\n"
        ",interesa,,Found Elsewhere,Associate,,https://other.test/job,,,\n"
        "a3,Llamada pendiente,,Beta,Banker,,,,,\n"
        "a4,aplicado,2026-09-31,Typo,Date,,,,2026-02-30 interesa,\n", encoding="utf-8")
    tracker = Tracker(tmp_path, "es", make_tz())
    changed = tracker.detect_manual(TZ_DAY)
    assert len(changed) == 4 and changed[0] == "a1" and changed[1].startswith("m-")
    tracker.save(NOW)
    rows = read_csv(tmp_path)
    assert rows[0]["historial"] == "2026-09-20 aplicado; 2026-09-26 Entrevista (manual)"
    assert rows[0]["notas"] == "llamar el lunes, 10:00" and rows[0]["salario"] == "90k"
    assert rows[1]["historial"] == f"{TZ_DAY} interesa (manual)" and rows[1]["fecha"] == TZ_DAY
    assert rows[2]["estado"] == "Llamada pendiente"  # unknown statuses are kept as typed
    # impossible dates typed by hand are replaced by the day the change was seen
    assert rows[3]["fecha"] == TZ_DAY and rows[3]["historial"].endswith(f"; {TZ_DAY} aplicado (manual)")
    assert tracker.feed(NOW, 14)["items"]  # feeds do not choke on them
    # nothing new on the next pass: no churn
    again = Tracker(tmp_path, "es", make_tz())
    assert again.detect_manual("2026-09-28") == [] and not again.changed
    assert again.counts() == {"interested": 1, "applied": 1, "interview": 1, "other": 1}


def test_english_headers_are_read_and_kept(tmp_path):
    path = tmp_path / "tracker" / "applications.csv"
    path.parent.mkdir()
    path.write_text("id,status,date,company,title,location,url,notes,history\n"
                    "a1,applied,2026-09-20,Acme,Analyst,,,,2026-09-20 applied\n", encoding="utf-8")
    tracker = Tracker(tmp_path, "es", make_tz())
    assert tracker.status_of("a1") == "applied"
    tracker.apply(TrackerOp("a1", "interview", TZ_DAY))
    tracker.save(NOW)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("id,status,date,") and "2026-09-20 applied; 2026-09-27 entrevista" in text


def test_concurrent_web_edit_and_button_are_merged(tmp_path):
    _, (a, b) = setup_repos(tmp_path)
    csv_a = a / "tracker" / "applications.csv"
    csv_a.parent.mkdir()
    csv_a.write_text("id,estado,fecha,empresa,puesto,ubicacion,url,notas,historial\n"
                     "x1,interesa,2026-09-20,Acme,Analyst,,,,2026-09-20 interesa\n", encoding="utf-8")
    assert commit_and_push(a, "tracker")
    git(b, "pull", "-q")

    tracker = Tracker(b, "es", make_tz())  # a run in b presses a button...
    tracker.apply(TrackerOp("y2", "applied", TZ_DAY, {"company": "Beta", "title": "Banker"}))
    tracker.save(NOW)
    # ...while the row x1 is edited on the GitHub website
    csv_a.write_text("id,estado,fecha,empresa,puesto,ubicacion,url,notas,historial\n"
                     "x1,aplicado,2026-09-27,Acme,Analyst,,,enviado CV,2026-09-20 interesa\n", encoding="utf-8")
    assert commit_and_push(a, "web edit")

    def rewrite():
        tracker.merge_with_disk(NOW)
        tracker.save(NOW)

    assert commit_and_push(b, "run b", rewrite=rewrite)
    git(a, "pull", "-q")
    rows = {r["id"]: r for r in read_csv(a)}
    assert rows["x1"]["notas"] == "enviado CV" and rows["x1"]["estado"] == "aplicado"
    assert rows["x1"]["historial"] == "2026-09-20 interesa; 2026-09-27 aplicado (manual)"
    assert rows["y2"]["estado"] == "aplicado" and rows["y2"]["empresa"] == "Beta"
    log = subprocess.run(["git", "-C", str(a), "log", "--oneline"], capture_output=True, text=True).stdout
    assert log.count("\n") == 4  # init, tracker, web edit, run b


# ------------------------------------------------------------ notifications
@respx.mock
async def test_per_job_messages_carry_buttons_and_are_remembered(tmp_path):
    routes = mock_telegram()
    counter = iter(range(200, 300))
    routes["send"].side_effect = lambda request: httpx.Response(
        200, json={"ok": True, "result": {"message_id": next(counter), "chat": {"id": 42}}})
    write_config(tmp_path)
    await run(None, tmp_path, groups=["company_sites"], now=NOW)
    sent = bodies(routes["send"])
    with_buttons = [p for p in sent if "reply_markup" in p]
    assert len(with_buttons) == 1 and "Product Manager DACH" in with_buttons[0]["text"]
    kb = with_buttons[0]["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["callback_data"] == callback_data("interested", JID)
    assert "reply_markup" not in sent[0]  # the header message has no buttons
    saved = json.loads((tmp_path / "state" / "tracker.json").read_text())
    assert saved["messages"][JID]["key"] == KEY and saved["messages"][JID]["refs"][0][:2] == ["42", 201]


@respx.mock
async def test_test_notify_shows_the_buttons(tmp_path):
    routes = mock_telegram()
    write_config(tmp_path)
    assert await send_test_notification(None, tmp_path) == 0
    payload = bodies(routes["send"])[0]
    assert "Botones de prueba" in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "jf1:interested:test"


@respx.mock
async def test_polling_run_saves_tracker_changes_only(tmp_path):
    """A favourites run with nothing new still saves a button press (and nothing else)."""
    write_config(tmp_path, favorite=True, jobs=[])
    routes = mock_telegram([button(1, callback_data("discarded", "0123456789ab"))])
    await run(None, tmp_path, groups=["favorites"], now=NOW)
    assert routes["updates"].call_count == 1
    assert read_csv(tmp_path)[0]["estado"] == "descartado"
