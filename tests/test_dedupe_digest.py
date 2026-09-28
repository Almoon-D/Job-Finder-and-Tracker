"""Fuzzy fingerprints, cross-group de-duplication, grouped digests and AI provider fallback."""

from __future__ import annotations

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from jobfinder.app import run
from jobfinder.app import test_ai as check_ai
from jobfinder.matching.llm import LLMMatcher
from jobfinder.matching.location import LocationMatcher
from jobfinder.models import Job, canonical_url, fuzzy_company, fuzzy_title
from jobfinder.notify.base import Notification
from jobfinder.notify.channels import Discord, Email, Telegram, pack_embeds
from jobfinder.sources.http import Http
from jobfinder.state import State

from .conftest import NOW, make_config

TZ = ZoneInfo("Europe/Madrid")


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


def test_fuzzy_keys():
    assert fuzzy_company("J.P. Morgan Chase & Co.") == fuzzy_company("JP Morgan Chase") == "jpmorganchase"
    assert fuzzy_company("Banco Ejemplo, S.A.") == fuzzy_company("banco ejemplo") == "bancoejemplo"
    LocationMatcher(make_config(locations=[{"city": "Geneva", "country": "CH"}]).locations)  # registers place words
    assert fuzzy_title("Private Banker (m/f/d) - Geneva") == fuzzy_title("Private Banker, Genève") \
        == fuzzy_title("Banker Private") == "banker private"
    assert fuzzy_title("Private Banker Assistant") != fuzzy_title("Private Banker")
    a = Job("s1", "1", "Private Banker (h/m)", "https://a.test/1", "Acme Private Bank SA", ["Geneva"])
    b = Job("s2", "9", "Private Banker", "https://b.test/9", "ACME Private Bank", ["Genève, Switzerland"])
    assert a.fingerprint == b.fingerprint
    no_company = Job("s3", "5", "Private Banker", "https://c.test/5", "")
    assert no_company.fingerprint != a.fingerprint  # unknown company never matches other offers


def test_canonical_url_keeps_ids_and_drops_tracking():
    assert canonical_url("https://es.indeed.com/viewjob?jk=abc&from=ja&utm_source=x") == "es.indeed.com/viewjob?jk=abc"
    assert canonical_url("https://www.linkedin.com/jobs/view/42/?trk=x") == "linkedin.com/jobs/view/42"


def write_config(tmp_path, sources: str, extra_groups: str = "") -> None:
    (tmp_path / "config.yaml").write_text(f"""
timezone: Europe/Madrid
language: es
role_families:
  - {{name: banca_privada, label: Banca privada, include_any: [private banker, banquero privado]}}
  - {{name: ir, include_any: [investor relations]}}
locations: [{{country: ES, label: España}}, {{city: Geneva, country: CH}}]
groups:
  company_sites: {{times: ["09:00"], format: per_job}}
  boards: {{times: ["11:00"], every_days: 3, max_age_days: 3.5, format: grouped, max_items: 3}}
{extra_groups}
sources:
{sources}
notify:
  telegram: {{enabled: true}}
""", encoding="utf-8")


SOURCES = """
  - name: Acme Private Bank
    type: fake
    jobs: [{id: "1", title: "Private Banker (m/f/d)", locations: ["Geneva, Switzerland"],
            posted: "2026-09-27T06:00:00+00:00"}]
  - name: Board One
    type: fake
    group: boards
    jobs:
      - {id: "a", title: "Private Banker", company: "ACME Private Bank SA", locations: ["Genève"],
         posted: "2026-09-27T06:00:00+00:00"}
      - {id: "b", title: "Banquero privado senior", company: "Banco Ficticio", locations: ["Madrid, Spain"],
         posted: "2026-09-27T06:00:00+00:00"}
      - {id: "c", title: "Investor Relations Manager", company: "Energía Ejemplo", locations: ["Madrid"],
         posted: "2026-09-27T06:00:00+00:00"}
      - {id: "d", title: "Private Banker", company: "Globex", locations: ["Geneva"], posted: "2026-09-27T06:00:00+00:00"}
      - {id: "e", title: "Banquero privado", company: "Initech", locations: ["Sevilla"], posted: "2026-09-26T06:00:00+00:00"}
  - name: Board Two
    type: fake
    group: boards
    jobs: [{id: "x", title: "Banquero Privado Senior", company: "Banco Ficticio S.A.", locations: ["Madrid"],
            posted: "2026-09-27T05:00:00+00:00"}]
"""


def texts() -> list[str]:
    return [json.loads(c.request.content)["text"] for c in respx.calls if "api.telegram.org" in str(c.request.url)]


@respx.mock
async def test_cross_group_duplicate_is_not_repeated_and_digest_is_grouped(tmp_path):
    respx.post(url__startswith="https://api.telegram.org/").mock(return_value=httpx.Response(200, json={"ok": True}))
    write_config(tmp_path, SOURCES)
    await run(None, tmp_path, groups=["company_sites"], now=NOW)
    assert "Private Banker" in "\n".join(texts())
    before = len(texts())

    await run(None, tmp_path, groups=["boards"], now=NOW + timedelta(hours=2))
    digest = "\n".join(texts()[before:])
    assert "ACME" not in digest  # same offer already sent by the company site: not repeated
    assert "🔁 1 ya avisadas en otros grupos" in digest
    # Board One and Board Two list the same offer: merged into one line
    assert digest.count("Banquero privado senior") + digest.count("Banquero Privado Senior") == 1
    assert "También en: Board Two" in digest or "También en: Board One" in digest
    # Sections: family order from the config, then place order from the config
    heads = [line for line in digest.splitlines() if line.startswith("<b>") and "·" in line]
    assert heads == ["<b>Banca privada · España (1)</b>", "<b>Banca privada · Geneva (1)</b>", "<b>Ir · España (1)</b>"]
    assert "… y 1 más en el feed" in digest  # 4 matches, max_items 3: the rest is in the feed
    state = State(tmp_path)
    assert state.seen["acme-private-bank:1"]["also_on"] == ["Board One"]
    feed = json.loads((tmp_path / "feeds" / "jobs.json").read_text())
    assert any("También en: Board One" in it["content_text"] for it in feed["items"])


def _jobs(n: int, family: str = "banca_privada", target: str = "España") -> list[Job]:
    out = []
    for i in range(n):
        j = Job("s", str(i), f"Private Banker {i} " + "x" * 80, f"https://jobs.example.test/{i}", "Acme",
                locations=["Madrid, Spain"], posted_at=NOW, posted_precision="datetime")
        j.family, j.score, j.extra["target"] = family, 80, target
        out.append(j)
    return out


def _grouped(jobs: list[Job]) -> Notification:
    return Notification("boards", "Portales", jobs, "es", TZ, NOW, format="grouped", max_items=len(jobs),
                        family_labels={"banca_privada": "Banca privada"}, place_order=["España", "Geneva"])


@respx.mock
async def test_grouped_digest_respects_telegram_and_discord_limits(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example.test/hook")
    tg = respx.post("https://api.telegram.org/bot123:abc/sendMessage").mock(return_value=httpx.Response(200, json={}))
    dc = respx.post("https://discord.example.test/hook").mock(return_value=httpx.Response(204))
    cfg = make_config(notify={"telegram": {"enabled": True}, "discord": {"enabled": True}})
    n = _grouped(_jobs(70) + _jobs(5, target="Geneva"))
    async with Http() as http:
        await Telegram(cfg, http).send(n)
        await Discord(cfg, http).send(n)
    sent = [json.loads(c.request.content)["text"] for c in tg.calls]
    assert len(sent) > 1 and all(len(s) <= 4096 for s in sent)
    assert sum(s.count("• <a href=") for s in sent) == 75
    assert not any(s.rstrip().endswith("</b>") for s in sent)  # a heading always travels with a job line
    for call in dc.calls:
        payload = json.loads(call.request.content)
        embeds = payload["embeds"]
        assert len(embeds) <= 10
        assert sum(len(e.get("title", "")) + len(e["description"]) + len(e.get("footer", {}).get("text", ""))
                   for e in embeds) <= 6000
        assert all(len(e["description"]) <= 4096 for e in embeds)
    titles = [e["title"] for c in dc.calls for e in json.loads(c.request.content)["embeds"]]
    assert titles[0] == "Banca privada · España (70)" and titles[-1] == "Banca privada · Geneva (5)"


def test_pack_embeds_limits():
    embeds = [{"title": "t", "description": "d" * 1500} for _ in range(12)]
    packs = pack_embeds(embeds)
    assert [len(p) for p in packs] == [3, 3, 3, 3]
    assert [len(p) for p in pack_embeds([{"title": "t", "description": "d"}] * 25)] == [10, 10, 5]


def test_grouped_email_has_sections():
    cfg = make_config(notify={"email": {"enabled": True}})
    text, html_body = Email(cfg, None).render(_grouped(_jobs(2) + _jobs(1, family="other", target="")))
    assert "== Banca privada · España (2) ==" in text and "Otros puestos (1)" in text
    assert "<h3" in html_body and "Banca privada · España (2)" in html_body


@respx.mock
async def test_ai_quota_error_moves_to_next_provider_without_retrying(monkeypatch):
    monkeypatch.setenv("KEY_A", "a")
    monkeypatch.setenv("KEY_B", "b")
    first = respx.post("https://a.example.test/v1/chat/completions").mock(return_value=httpx.Response(429))
    answer = {"results": [{"id": "0", "score": 88, "family": "product", "reason": "ok"}]}
    second = respx.post("https://b.example.test/v1/chat/completions").mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"content": json.dumps(answer)}}]}))
    cfg = make_config(llm={"enabled": True, "batch_size": 1, "providers": [
        {"name": "a", "base_url": "https://a.example.test/v1", "model": "big", "api_key_env": "KEY_A",
         "extra_body": {"reasoning_effort": "low"}},
        {"name": "b", "base_url": "https://b.example.test/v1", "model": "small", "api_key_env": "KEY_B"}]})
    jobs = [Job("s", str(i), "Product Manager", f"https://j.test/{i}", "Acme") for i in range(2)]
    async with Http() as http:
        verdicts = await LLMMatcher(cfg, http).score(jobs)
    assert first.call_count == 1  # 429: no retries, and not asked again for the second batch
    assert json.loads(first.calls[0].request.content)["reasoning_effort"] == "low"
    assert second.call_count == 2 and {v.score for v in verdicts.values()} == {88}


@respx.mock
async def test_test_ai_reports_status_per_provider(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KEY_A", "a")
    monkeypatch.delenv("KEY_B", raising=False)
    respx.post("https://a.example.test/v1/chat/completions").mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"content": '{"ok": true}'}}]}))
    (tmp_path / "config.yaml").write_text("""
groups: {g: {times: ["09:00"]}}
llm:
  enabled: true
  providers:
    - {name: a, base_url: "https://a.example.test/v1", model: m, api_key_env: KEY_A}
    - {name: b, base_url: "https://b.example.test/v1", model: m, api_key_env: KEY_B}
""")
    assert await check_ai(None, tmp_path) == 0
    err = capsys.readouterr().err
    assert "provider #1 (a): OK" in err and "1 provider(s) without API key skipped" in err
