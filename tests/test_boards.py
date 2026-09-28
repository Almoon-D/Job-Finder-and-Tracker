"""Job-board adapters against synthetic fixtures (no network)."""

from __future__ import annotations

import httpx
import pytest
import respx

from jobfinder.config.schema import LocationSpec, Source
from jobfinder.matching.location import LocationMatcher
from jobfinder.sources.base import FetchContext, SkipSource, build_adapter
from jobfinder.sources.http import Http
from jobfinder.state import State

from .conftest import NOW, fixture_text, make_config

LOCATIONS = [{"country": "ES"}, {"city": "Geneva", "country": "CH"}, {"city": "Lausanne", "country": "CH"},
             {"city": "Mexico City", "country": "MX"}]


def ctx_for(tmp_path, max_age=3.5, **source_kwargs):
    cfg = make_config(locations=LOCATIONS, coverage={"text_any": ["Iberia"], "search_terms": ["Iberia"]})
    source = Source(name=source_kwargs.pop("name", "Board"), group="company_sites", **source_kwargs)
    http = Http(cfg.http, transport=httpx.AsyncHTTPTransport())
    locs = LocationMatcher([LocationSpec(**x) for x in LOCATIONS], cfg.coverage)
    return FetchContext(http, cfg, source, State(tmp_path), NOW, locs, max_age)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)


@respx.mock
async def test_efinancialcareers_newest_per_country(tmp_path):
    route = respx.get(url__startswith="https://job-search-api.efinancialcareers.com/").mock(
        return_value=httpx.Response(200, text=fixture_text("efc_search.json")))
    ctx = ctx_for(tmp_path, type="efinancialcareers", countries=["CH"])
    jobs = await build_adapter(ctx).fetch()
    params = [dict(c.request.url.params) for c in route.calls]
    country = next(p for p in params if p.get("countryCode2") == "CH")
    assert country["sortBy"] == "POSTED_DATE" and country["q"] == "" and country["locationPrecision"] == "Country"
    assert any(p["q"] == "Iberia" and "countryCode2" not in p for p in params)  # worldwide coverage search
    pb = next(j for j in jobs if j.native_id == "efcA1")
    assert pb.url == "https://www.efinancialcareers.com/jobs-Switzerland-Geneva-Private_Banker.id111111"
    assert pb.company == "Acme Private Bank" and pb.locations == ["Geneva, Switzerland"]
    assert pb.description == "Advise UHNW clients from Spain." and pb.posted_precision == "datetime"
    await ctx.http.aclose()


@respx.mock
async def test_jobcloud_queries_places_and_detail(tmp_path):
    search = respx.get("https://www.jobup.ch/api/v1/public/search").mock(
        return_value=httpx.Response(200, text=fixture_text("jobcloud_search.json")))
    respx.get(url__startswith="https://www.jobup.ch/fr/emplois/detail/").mock(
        return_value=httpx.Response(200, text=fixture_text("jobcloud_detail.html")))
    ctx = ctx_for(tmp_path, type="jobcloud", site="jobup.ch", queries=["gestionnaire de fortune"],
                  use_coverage_search=False)
    adapter = build_adapter(ctx)
    jobs = await adapter.fetch()
    places = [c.request.url.params["location"] for c in search.calls]
    assert places == ["Geneva", "Lausanne"]  # the configured Swiss cities
    assert all(c.request.url.params["sort-by"] == "date" for c in search.calls)
    job = next(j for j in jobs if j.title == "Gestionnaire de fortune senior")
    assert job.company == "Acme Private Bank" and job.locations == ["Genève, Switzerland"]
    assert job.url.endswith("/fr/emplois/detail/00000001-aaaa-bbbb-cccc-dddddddddddd/")
    await adapter.enrich(job)
    assert "clientèle ibérique" in job.description
    await ctx.http.aclose()


async def test_jobcloud_rejects_unknown_site(tmp_path):
    ctx = ctx_for(tmp_path, type="jobcloud", site="example.test")
    with pytest.raises(Exception, match="site must be"):
        await build_adapter(ctx).fetch()
    await ctx.http.aclose()


@pytest.mark.parametrize("kind", ["infojobs", "adzuna"])
async def test_keyed_boards_skip_without_credentials(tmp_path, monkeypatch, kind):
    for name in ("INFOJOBS_CLIENT_ID", "INFOJOBS_CLIENT_SECRET", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
        monkeypatch.delenv(name, raising=False)
    ctx = ctx_for(tmp_path, type=kind, queries=["banca privada"])
    with pytest.raises(SkipSource, match="no credentials"):
        await build_adapter(ctx).fetch()
    await ctx.http.aclose()


@respx.mock
async def test_infojobs(tmp_path, monkeypatch):
    monkeypatch.setenv("INFOJOBS_CLIENT_ID", "cid")
    monkeypatch.setenv("INFOJOBS_CLIENT_SECRET", "sec")
    route = respx.get("https://api.infojobs.net/api/9/offer").mock(
        return_value=httpx.Response(200, text=fixture_text("infojobs.json")))
    ctx = ctx_for(tmp_path, type="infojobs", queries=["banca privada"], use_coverage_search=False)
    jobs = await build_adapter(ctx).fetch()
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Basic Y2lkOnNlYw=="  # base64("cid:sec")
    assert req.url.params["sinceDate"] == "_7_DAYS" and req.url.params["q"] == "banca privada"
    bp = next(j for j in jobs if j.native_id == "abc123")
    assert bp.company == "Banco Ficticio" and bp.locations == ["Madrid, Madrid, España"]
    assert "3 años" in bp.description
    await ctx.http.aclose()


@respx.mock
async def test_adzuna(tmp_path, monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    route = respx.get(url__regex=r"https://api\.adzuna\.com/v1/api/jobs/(es|ch|mx)/search/1").mock(
        return_value=httpx.Response(200, text=fixture_text("adzuna.json")))
    ctx = ctx_for(tmp_path, type="adzuna", queries=["private banker"], where={"ES": ["Madrid"]})
    jobs = await build_adapter(ctx).fetch()
    wheres = {(c.request.url.path.split("/")[4], c.request.url.params.get("where")) for c in route.calls}
    assert wheres == {("es", "Madrid"), ("ch", None), ("mx", None)}
    params = route.calls[0].request.url.params
    assert params["max_days_old"] == "4" and params["sort_by"] == "date"
    job = jobs[0]
    assert job.title == "Private Banker" and job.locations == ["Madrid, Comunidad de Madrid, España"]
    await ctx.http.aclose()


@respx.mock
async def test_html_list_cards_without_links_company_and_slug(tmp_path):
    route = respx.get(url__startswith="https://www.occ.com.mx/").mock(
        return_value=httpx.Response(200, text=fixture_text("occ_list.html")))
    ctx = ctx_for(tmp_path, type="html_list", queries=["Banca Privada"],
                  url="https://www.occ.com.mx/empleos/de-{query_slug}/en-ciudad-de-mexico/?tm=3&sort=2",
                  item="div[id^=jobcard-]", title="h2", id_attr="data-id",
                  url_template="https://www.occ.com.mx/empleo/oferta/{id}/", company_selector=".line-clamp-title a",
                  location="p.text-sm", date="span.text-sm")
    jobs = await build_adapter(ctx).fetch()
    assert route.calls[0].request.url.path == "/empleos/de-banca-privada/en-ciudad-de-mexico/"
    first = next(j for j in jobs if j.native_id == "900001")
    assert first.url == "https://www.occ.com.mx/empleo/oferta/900001/"
    assert first.title == "ASESOR PATRIMONIAL" and first.company == "BANCO FICTICIO"
    assert first.locations == ["Miguel Hidalgo, Ciudad de México"] and first.posted_precision == "relative"
    await ctx.http.aclose()


@respx.mock
async def test_html_list_computrabajo_recipe(tmp_path):
    respx.get(url__startswith="https://mx.computrabajo.com/").mock(
        return_value=httpx.Response(200, text=fixture_text("computrabajo_list.html")))
    ctx = ctx_for(tmp_path, type="html_list", queries=["banca privada"],
                  url="https://mx.computrabajo.com/trabajo-de-{query_slug}-en-cdmx?pubdate=3",
                  item="article.box_offer", title="h2 a.js-o-link", link="h2 a.js-o-link",
                  company_selector="a[offer-grid-article-company-url]", location="p.fs16 span.mr10", date="p.fc_aux")
    jobs = await build_adapter(ctx).fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.url.startswith("https://mx.computrabajo.com/ofertas-de-trabajo/oferta-de-trabajo-de-banquero-privado")
    assert j.company == "Banco Ficticio SA de CV" and j.posted_at is not None
    await ctx.http.aclose()


async def test_only_queries_skips_full_listing(tmp_path):
    ctx = ctx_for(tmp_path, url="https://acme.wd3.myworkdayjobs.com/External", queries=["investor relations"],
                  only_queries=True)
    adapter = build_adapter(ctx)
    assert not adapter.full_listing and adapter.coverage_terms() == []
    await ctx.http.aclose()


@respx.mock
async def test_jobcloud_skips_blocked_searches(tmp_path):
    responses = iter([httpx.Response(403), httpx.Response(200, text=fixture_text("jobcloud_search.json"))])
    respx.get("https://www.jobs.ch/api/v1/public/search").mock(side_effect=lambda req: next(responses))
    ctx = ctx_for(tmp_path, type="jobcloud", site="jobs.ch", queries=["private banker"], locations=["Geneva", "Lausanne"],
                  use_coverage_search=False)
    ctx.http.cfg.retries = 0
    jobs = await build_adapter(ctx).fetch()
    assert len(jobs) == 2  # the first search was blocked, the second one worked
    await ctx.http.aclose()


@respx.mock
async def test_jobcloud_fails_when_every_search_is_blocked(tmp_path):
    respx.get("https://www.jobs.ch/api/v1/public/search").mock(return_value=httpx.Response(403))
    ctx = ctx_for(tmp_path, type="jobcloud", site="jobs.ch", queries=["private banker"], locations=["Geneva"],
                  use_coverage_search=False)
    ctx.http.cfg.retries = 0
    with pytest.raises(Exception, match="every search was blocked"):
        await build_adapter(ctx).fetch()
    await ctx.http.aclose()
