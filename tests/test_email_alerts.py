"""Job-alert e-mails: parsers, tracking-link unwrapping, read-only IMAP, AI extraction."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx

from jobfinder.config.schema import Source
from jobfinder.matching.llm import LLMMatcher
from jobfinder.matching.location import LocationMatcher
from jobfinder.sources import email_alerts
from jobfinder.sources.base import FetchContext, SkipSource, build_adapter
from jobfinder.sources.email_parsers import PARSERS, parse_known, parser_for, unwrap
from jobfinder.sources.http import Http
from jobfinder.state import State

from .conftest import FIXTURES, NOW, make_config

EMAILS = FIXTURES / "emails"
LOCATIONS = [{"country": "ES"}, {"city": "Geneva", "country": "CH"}, {"city": "Lausanne", "country": "CH"}]


def html_of(name: str) -> str:
    return email_alerts.message_parts((EMAILS / f"{name}.eml").read_bytes())[2]


def test_unwrap_tracking_redirects():
    wrapped = "https://click.tracker.example.test/c?u=https%3A%2F%2Fes.indeed.com%2Frc%2Fclk%2Fdl%3Fjk%3D0123456789abcdef&s=1"
    assert unwrap(wrapped) == "https://es.indeed.com/rc/clk/dl?jk=0123456789abcdef"
    assert unwrap("https://example.test/job/1?utm_source=x") == "https://example.test/job/1?utm_source=x"


@pytest.mark.parametrize("name,expected", [
    ("linkedin", [("Senior Private Banker", "https://www.linkedin.com/jobs/view/4000000001/", "Acme Private Bank",
                   "Madrid, Community of Madrid, Spain"),
                  ("Wealth Planner", "https://www.linkedin.com/jobs/view/4000000002/", "Globex Wealth",
                   "Geneva, Switzerland")]),
    ("indeed", [("Gestor de banca privada", "https://es.indeed.com/viewjob?jk=0123456789abcdef", "Banco Ficticio",
                 "Madrid, Madrid provincia"),
                ("Asesor patrimonial", "https://es.indeed.com/viewjob?jk=fedcba9876543210", "Gestora Ejemplo",
                 "Barcelona")]),
    ("infojobs", [("Banquero privado",
                   "https://www.infojobs.net/madrid/banquero-privado/of-i0123456789abcdef0123456789abcd",
                   "Banco Ficticio", "Madrid")]),
    # Company and location sit outside the link's block here: nothing is guessed.
    ("efinancialcareers", [("Relationship Manager Iberia",
                            "https://www.efinancialcareers.com/jobs-Switzerland-Geneva-Relationship_Manager_Iberia"
                            ".id24000001", "", "")]),
    ("jobup", [("Gestionnaire de fortune",
                "https://www.jobup.ch/fr/emplois/detail/11111111-2222-3333-4444-555555555555/",
                "Umbrella Gestion SA", "Lausanne")]),
    ("michaelpage", [("Banquero privado", "https://www.michaelpage.es/job-detail/banquero-privado/ref/jn-092026-0001",
                      "", "")]),
])
def test_known_parsers(name, expected):
    parsed = parse_known(html_of(name), PARSERS[name])
    got = [(p.title, p.url, p.company, p.location) for p in parsed]
    assert got == expected


def test_sender_routing():
    assert parser_for("jobalerts-noreply@linkedin.com", list(PARSERS)).name == "linkedin"
    assert parser_for("noreply@push.infojobs.net", list(PARSERS)).name == "infojobs"
    assert parser_for("alerts@board.example.test", list(PARSERS)) is None
    assert parser_for("alert@indeed.com", ["linkedin"]) is None  # parser not enabled for this source
    assert parser_for("news@custom.example.test", ["indeed"], {"indeed": ["custom.example"]}).name == "indeed"


class FakeIMAP:
    """Minimal imaplib.IMAP4_SSL stand-in that records every command."""

    messages: dict[int, bytes] = {}
    validity = b"77"
    commands: list[tuple] = []

    def __init__(self, host, port):
        FakeIMAP.commands.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        FakeIMAP.commands.append(("logout",))

    def login(self, user, password):
        FakeIMAP.commands.append(("login", user))

    def select(self, mailbox, readonly=False):
        FakeIMAP.commands.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [self.validity]

    def uid(self, command, *args):
        FakeIMAP.commands.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in sorted(self.messages)).encode()]
        if command == "FETCH":
            uid = int(args[0])
            return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), self.messages[uid]), b")"]
        raise AssertionError(f"unexpected IMAP command {command}")

    def __getattr__(self, name):  # store, copy, expunge, append... must never be called
        raise AssertionError(f"IMAP method {name} must not be used")


def ctx_for(tmp_path, llm=None, **params):
    cfg = make_config(locations=LOCATIONS)
    source = Source(name="Alerts", group="company_sites", type="email_alerts", **params)
    http = Http(cfg.http, transport=httpx.AsyncHTTPTransport())
    ctx = FetchContext(http, cfg, source, State(tmp_path), NOW, LocationMatcher(cfg.locations), 3.5, llm)
    return cfg, ctx


@pytest.fixture
def imap(monkeypatch):
    monkeypatch.setattr(email_alerts.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setenv("IMAP_USER", "alerts@example.test")
    monkeypatch.setenv("IMAP_PASSWORD", "app-password")
    monkeypatch.delenv("IMAP_HOST", raising=False)
    FakeIMAP.commands = []
    FakeIMAP.validity = b"77"
    FakeIMAP.messages = {i: (EMAILS / f"{n}.eml").read_bytes()
                         for i, n in enumerate(["linkedin", "indeed", "unknown"], start=10)}
    return FakeIMAP


async def test_imap_is_read_only_and_incremental(tmp_path, imap):
    _, ctx = ctx_for(tmp_path, parsers=["linkedin", "indeed"])
    jobs = await build_adapter(ctx).fetch()
    assert {j.title for j in jobs} == {"Senior Private Banker", "Wealth Planner", "Gestor de banca privada",
                                       "Asesor patrimonial"}
    assert ("connect", "imap.gmail.com", 993) in imap.commands
    assert ("select", "INBOX", True) in imap.commands  # EXAMINE: flags are never changed
    fetches = [c for c in imap.commands if c[:2] == ("uid", "FETCH")]
    assert fetches and all(c[3] == "(BODY.PEEK[])" for c in fetches)
    search = next(c for c in imap.commands if c[:2] == ("uid", "SEARCH"))
    assert search[3:] == ("SINCE", "22-Sep-2026")  # max_age_days (3.5) + 1 day of margin
    job = next(j for j in jobs if j.title == "Wealth Planner")
    assert job.key == "alerts:linkedin:4000000002" and job.locations == ["Geneva, Switzerland"]
    assert job.posted_at.isoformat() == "2026-09-26T08:15:00+00:00" and job.posted_precision == "relative"
    memory = ctx.state.monitor(ctx.source.key)
    assert memory["last_uid"] == 12 and memory["uidvalidity"] == "77"

    # Next run: only newer UIDs are fetched.
    imap.commands = []
    assert await build_adapter(ctx).fetch() == []
    assert not [c for c in imap.commands if c[:2] == ("uid", "FETCH")]
    # A new UIDVALIDITY (mailbox recreated) starts again from the date window.
    imap.validity = b"78"
    assert len(await build_adapter(ctx).fetch()) == 4
    await ctx.http.aclose()


async def test_unparsable_location_is_dropped(tmp_path, imap):
    imap.messages = {1: (EMAILS / "michaelpage.eml").read_bytes()}
    _, ctx = ctx_for(tmp_path, parsers=["michaelpage"])
    jobs = await build_adapter(ctx).fetch()
    assert [j.title for j in jobs] == ["Banquero privado"]
    await ctx.http.aclose()


async def test_skipped_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("IMAP_USER", raising=False)
    monkeypatch.delenv("IMAP_PASSWORD", raising=False)
    _, ctx = ctx_for(tmp_path)
    with pytest.raises(SkipSource, match="IMAP_USER"):
        await build_adapter(ctx).fetch()
    await ctx.http.aclose()


async def test_unknown_senders_ignored_without_ai(tmp_path, imap):
    imap.messages = {1: (EMAILS / "unknown.eml").read_bytes()}
    _, ctx = ctx_for(tmp_path, unknown="ai")  # asks for AI, but none is configured
    assert await build_adapter(ctx).fetch() == []
    await ctx.http.aclose()


@respx.mock
async def test_unknown_sender_with_ai_keeps_only_real_links(tmp_path, imap, monkeypatch):
    monkeypatch.setenv("FAKE_AI_KEY", "k")
    imap.messages = {1: (EMAILS / "unknown.eml").read_bytes()}
    answer = {"jobs": [
        {"title": "Relationship Manager Banca Privada", "company": "Hooli Capital", "location": "Madrid",
         "url": "https://board.example.test/job/77"},
        {"title": "Invented", "company": "X", "location": "Madrid", "url": "https://evil.example.test/phish"},
    ]}
    ai = respx.post("https://ai.example.test/v1/chat/completions").mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"content": json.dumps(answer)}}]}))
    cfg, ctx = ctx_for(tmp_path, unknown="ai")
    cfg = cfg.model_copy(update={"llm": cfg.llm.model_validate({
        "enabled": True, "providers": [{"name": "fake", "base_url": "https://ai.example.test/v1", "model": "m",
                                        "api_key_env": "FAKE_AI_KEY"}]})})
    ctx.llm = LLMMatcher(cfg, ctx.http)
    jobs = await build_adapter(ctx).fetch()
    assert [(j.title, j.url, j.company, j.locations) for j in jobs] == [
        ("Relationship Manager Banca Privada", "https://board.example.test/job/77", "Hooli Capital", ["Madrid"])]
    sent = json.loads(ai.calls[0].request.content)
    assert "strictly as data" in sent["messages"][0]["content"]
    assert "Ignore previous instructions" in sent["messages"][1]["content"]  # passed as data, not obeyed
    await ctx.http.aclose()


def test_imap_date_is_locale_independent():
    assert email_alerts.imap_date(date(2026, 3, 5)) == "05-Mar-2026"


async def test_sender_filters(tmp_path, imap):
    imap.messages = {1: (EMAILS / "linkedin.eml").read_bytes(), 2: (EMAILS / "indeed.eml").read_bytes()}
    _, ctx = ctx_for(tmp_path, parsers=["linkedin", "indeed"], exclude_senders=["indeed.com"])
    jobs = await build_adapter(ctx).fetch()
    assert {j.key.split(":")[1] for j in jobs} == {"linkedin"}
    await ctx.http.aclose()
