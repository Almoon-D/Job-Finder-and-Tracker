"""Weekly summary: scheduling, content, Markdown report and delivery."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from jobfinder.app import run
from jobfinder.state import State, iso
from jobfinder.stats import RunStats, sum_days

SUNDAY_18 = datetime(2026, 9, 27, 16, 5, tzinfo=UTC)  # Sunday 18:05 in Berlin (CEST)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    async def no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)

CONFIG = """
timezone: Europe/Berlin
language: es
role_families:
  - {name: producto, label: Producto, include_any: [product manager]}
  - {name: datos, label: Datos, include_any: [data analyst]}
locations: [{country: DE}]
groups:
  company_sites: {times: ["09:00", "20:30"], format: per_job}
  boards: {times: ["11:00"], every_days: 3, format: grouped}
  weekly_summary: {kind: summary, times: ["18:00"], weekdays: [sun]}
sources:
  - {name: Bank One, type: fake, jobs: []}
  - {name: Bank Two, type: fake, jobs: []}
  - {name: Board, type: fake, group: boards, jobs: []}
notify:
  telegram: {enabled: true}
  discord: {enabled: true}
"""


def seed(tmp_path) -> None:
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    state = State(tmp_path)
    day = SUNDAY_18 - timedelta(days=2)

    def entry(key, company, group, when, family=None, **extra):
        state.seen[key] = {"first_seen": iso(when), "last_seen": iso(when), "title": "T", "company": company,
                           "url": f"https://x.test/{key}", "notified": {group: iso(when)}, **extra}
        if family:
            state.seen[key]["family"] = family

    entry("bank-one:1", "Bank One", "company_sites", day, "producto")
    entry("bank-one:2", "Bank One", "company_sites", day, verdict={"family": "datos", "score": 80})
    entry("board:1", "Bank Three", "boards", day, "producto")
    entry("board:2", "Bank One", "boards", day, "producto", dup_of="bank-one:1")  # folded: not counted
    entry("bank-two:1", "Bank Two", "company_sites", SUNDAY_18 - timedelta(days=9))  # previous week
    state.runs["sources"] = {"bank-one": {"fail_streak": 0, "counts": [5, 5]},
                             "bank-two": {"fail_streak": 3, "last_error": "HTTP 503"},
                             "board": {"fail_streak": 0, "counts": [5, 5]}}
    state.save()
    stats = {"days": {
        "2026-09-25": {"groups": {"company_sites": {"runs": 2, "fetched": 300, "candidates": 20, "matches": 2,
                                                    "rejected": {"location": 200, "already_notified": 70,
                                                                 "ai_score": 12, "duplicate_previous": 3}}},
                       "sources": {"bank-one": {"runs": 2, "ok": 2, "jobs": 250, "seconds": 20, "matches": 2},
                                   "bank-two": {"runs": 2, "ok": 0, "jobs": 0, "seconds": 60}}},
        "2026-09-10": {"groups": {"company_sites": {"runs": 9, "fetched": 9999, "matches": 99}}},  # outside
    }}
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "stats.json").write_text(json.dumps(stats), encoding="utf-8")
    (tmp_path / "tracker").mkdir()
    (tmp_path / "tracker" / "applications.csv").write_text(
        "id,estado,fecha,empresa,puesto,ubicacion,url,notas,historial\n"
        "a1,aplicado,2026-09-24,Bank One,PB,,,,2026-09-20 interesa; 2026-09-24 aplicado\n"
        "a2,interesa,2026-09-01,Bank Two,IR,,,,2026-09-01 interesa\n", encoding="utf-8")


def test_stats_sum_days_includes_current_run(tmp_path):
    seed(tmp_path)
    current = RunStats()
    current.add_group("boards", {"fetched": 10, "candidates": 3, "matches": 1, "rejected": {"location": 7}},
                      {"board": {"ok": True, "jobs": 10, "seconds": 1.5, "matches": 1}})
    total = sum_days(tmp_path, datetime(2026, 9, 21).date(), datetime(2026, 9, 27).date(), current)
    assert total["groups"]["company_sites"]["fetched"] == 300 and total["groups"]["boards"]["matches"] == 1
    assert total["sources"]["board"] == {"runs": 1, "ok": 1, "jobs": 10, "seconds": 1.5, "matches": 1}
    current.merge_into(tmp_path, datetime(2026, 9, 27).date())
    current.merge_into(tmp_path, datetime(2026, 9, 27).date())
    saved = json.loads((tmp_path / "runs" / "stats.json").read_text())
    assert saved["days"]["2026-09-27"]["groups"]["boards"]["runs"] == 2
    assert "2026-09-10" in saved["days"]  # kept for 35 days


@respx.mock
async def test_weekly_summary_is_sent_and_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example.test/hook")
    respx.post("https://api.telegram.org/bot123:abc/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []}))
    tg = respx.post("https://api.telegram.org/bot123:abc/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    dc = respx.post("https://discord.example.test/hook").mock(return_value=httpx.Response(204))
    seed(tmp_path)

    assert await run(None, tmp_path, groups=["weekly_summary"], now=SUNDAY_18) == 0
    text = "\n".join(json.loads(c.request.content)["text"] for c in tg.calls)
    assert "📊 Resumen semanal 2026-W39" in text
    assert "Ofertas nuevas: 3" in text
    assert "Por empresa: Bank One 2 · Bank Three 1" in text
    assert "Por familia: Producto 2 · Datos 1" in text
    assert "2 coincidencias de 300 ofertas revisadas" in text
    assert "Descartes: ubicación 200 · encaje IA bajo 12 · duplicadas 3" in text
    assert "ya avisada" not in text and "already_notified" not in text
    assert "⭐ Interesa 1 · ✅ Aplicado 1" in text and "Esta semana: +1 ✅ Aplicado" in text
    assert "1/2 fuentes funcionando" in text and "Bank Two (fail x3: HTTP 503)" in text
    assert "reports/weekly/2026-W39.md" in text
    embeds = [e["title"] for c in dc.calls for e in json.loads(c.request.content).get("embeds", [])]
    assert len(embeds) == 4 and embeds[0].startswith("🆕")

    md = (tmp_path / "reports" / "weekly" / "2026-W39.md").read_text(encoding="utf-8")
    assert md.startswith("# 📊 Resumen semanal 2026-W39")
    assert "| Bank One | 2 |" in md and "| ubicación | 200 |" in md
    assert "| Bank One | 2 | 2 | 125.0 | 2 | 10.0 | OK |" in md
    assert "| Bank Two | 2 | 0 | 0.0 | 0 | 30.0 | fail x3: HTTP 503 |" in md

    # Idempotent: the same slot is not sent twice; Monday is not a slot
    tg.reset()
    await run(None, tmp_path, groups=["weekly_summary"], now=SUNDAY_18 + timedelta(minutes=40))
    await run(None, tmp_path, groups=["weekly_summary"], now=SUNDAY_18 + timedelta(days=1))
    assert tg.call_count == 0


@respx.mock
async def test_failed_summary_is_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    respx.post("https://api.telegram.org/bot123:abc/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []}))
    tg = respx.post("https://api.telegram.org/bot123:abc/sendMessage").mock(return_value=httpx.Response(500))
    seed(tmp_path)
    cfg = (tmp_path / "config.yaml").read_text().replace("  discord: {enabled: true}\n", "")
    (tmp_path / "config.yaml").write_text(cfg)
    assert await run(None, tmp_path, groups=["weekly_summary"], now=SUNDAY_18) == 1
    assert not (tmp_path / "reports").exists()
    tg.mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await run(None, tmp_path, groups=["weekly_summary"], now=SUNDAY_18 + timedelta(hours=1)) == 0
    assert (tmp_path / "reports" / "weekly" / "2026-W39.md").exists()


def test_every_rejection_reason_has_a_label_in_both_languages():
    from jobfinder.i18n import MESSAGES
    from jobfinder.weekly import REASON_KEYS

    assert "source_family" in REASON_KEYS
    for lang in ("es", "en"):
        assert all(key in MESSAGES[lang] for key in REASON_KEYS.values()), lang
