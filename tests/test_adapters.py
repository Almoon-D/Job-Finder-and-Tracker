"""Adapters against synthetic HTTP fixtures (no network)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from jobfinder.config.schema import Source
from jobfinder.matching.location import LocationMatcher
from jobfinder.sources.base import FetchContext, build_adapter
from jobfinder.sources.detect import detect_url
from jobfinder.sources.http import Http
from jobfinder.state import State

from .conftest import NOW, fixture_text, make_config


def ctx_for(tmp_path, **source_kwargs):
    cfg = make_config(coverage={"text_any": ["DACH"], "search_terms": []})
    source = Source(name=source_kwargs.pop("name", "Acme"), group="company_sites", **source_kwargs)
    http = Http(cfg.http, transport=httpx.AsyncHTTPTransport())
    return FetchContext(http, cfg, source, State(tmp_path), NOW, LocationMatcher(cfg.locations, cfg.coverage), 3.0)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://acme.wd3.myworkdayjobs.com/en-US/External", ("workday", {"tenant": "acme", "site": "External"})),
        ("https://wd3.myworkdaysite.com/recruiting/acme/Careers", ("workday", {"tenant": "acme", "site": "Careers"})),
        ("https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions",
         ("oracle_hcm", {"site": "CX_1"})),
        ("https://acme.fa.ocs.oraclecloud.eu/hcmUI/CandidateExperience/en/sites/CX_1/jobs?mode=location",
         ("oracle_hcm", {"host": "acme.fa.ocs.oraclecloud.eu", "site": "CX_1"})),
        ("https://acme.fa.oraclecloud.com:443/hcmUI/CandidateExperience/en/sites/CX_1/jobs",
         ("oracle_hcm", {"host": "acme.fa.oraclecloud.com:443", "site": "CX_1"})),
        ("https://acme.eightfold.ai/careers", ("eightfold", {"domain": "acme.com"})),
        ("https://boards.greenhouse.io/acme", ("greenhouse", {"board": "acme"})),
        ("https://jobs.lever.co/acme", ("lever", {"company": "acme"})),
        ("https://jobs.smartrecruiters.com/Acme", ("smartrecruiters", {"company": "Acme"})),
        ("https://jobs.ashbyhq.com/acme", ("ashby", {"board": "acme"})),
        ("https://apply.workable.com/acme/", ("workable", {"account": "acme"})),
        ("https://acme.recruitee.com/", ("recruitee", {"company": "acme"})),
        ("https://career5.successfactors.eu/career?company=acmecorp", ("successfactors", {"company": "acmecorp"})),
    ],
)
def test_detect(url, expected):
    name, params = detect_url(url)
    assert name == expected[0]
    for k, v in expected[1].items():
        assert params[k] == v


def test_detect_unknown():
    assert detect_url("https://example.test/careers") is None


@respx.mock
async def test_workday(tmp_path):
    api = "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External"
    route = respx.post(f"{api}/jobs").mock(return_value=httpx.Response(200, text=fixture_text("workday_jobs.json")))
    respx.get(f"{api}/job/Lisbon/Data-Analyst_R101").mock(
        return_value=httpx.Response(200, text=fixture_text("workday_detail.json")))
    ctx = ctx_for(tmp_path, url="https://acme.wd3.myworkdayjobs.com/External")
    adapter = build_adapter(ctx)
    jobs = await adapter.fetch()
    # Second request must use the Berlin + Lisbon facets, not London
    body = json.loads(route.calls[1].request.content)
    assert body["appliedFacets"] == {"locations": ["loc-ber", "loc-lis"]}
    assert {j.title for j in jobs} == {"Product Manager - DACH", "Data Analyst", "Software Engineer"}
    wm = next(j for j in jobs if j.title == "Data Analyst")
    assert wm.extra["multi_location"] and wm.url.endswith("/External/job/Lisbon/Data-Analyst_R101")
    assert wm.posted_precision == "relative"
    await adapter.enrich(wm)
    assert wm.locations == ["Lisbon", "Porto"] and "B2B" in wm.description
    await ctx.http.aclose()


@respx.mock
async def test_workday_country_facet_reaches_configured_cities(tmp_path):
    """A tenant whose only location facet is the country ('Switzerland') still yields its Geneva jobs."""
    api = "https://bank.wd3.myworkdayjobs.com/wday/cxs/bank/Careers"
    facets = [{"facetParameter": "Location", "values": [{"descriptor": "Switzerland", "id": "ch", "count": 2},
                                                        {"descriptor": "United States", "id": "us", "count": 9}]}]
    postings = [{"title": "Relationship Manager - Romandie", "externalPath": "/job/Geneve/RM_1",
                 "locationsText": "Genève / Rue du Rhône 31", "postedOn": "Posted Today"},
                {"title": "Analyst", "externalPath": "/job/Zurich/A_2", "locationsText": "Zurich",
                 "postedOn": "Posted Today"}]
    route = respx.post(f"{api}/jobs").mock(side_effect=lambda request: httpx.Response(
        200, json={"total": 2, "jobPostings": postings if json.loads(request.content)["appliedFacets"] else [],
                   "facets": facets}))
    cfg_locations = [{"city": "Geneva", "country": "CH", "aliases": ["Genève"]}]
    ctx = ctx_for(tmp_path, url="https://bank.wd3.myworkdayjobs.com/Careers")
    ctx.locations = LocationMatcher(make_config(locations=cfg_locations).locations, ctx.config.coverage)
    jobs = await build_adapter(ctx).fetch()
    assert json.loads(route.calls[1].request.content)["appliedFacets"] == {"Location": ["ch"]}
    assert {j.title for j in jobs} == {"Relationship Manager - Romandie", "Analyst"}  # narrowed later by location
    assert ctx.locations.match_location("Genève / Rue du Rhône 31") and not ctx.locations.match_location("Zurich")
    await ctx.http.aclose()


@respx.mock
async def test_oracle(tmp_path):
    respx.get(url__regex=r"https://acme\.fa\.oraclecloud\.com/hcmRestApi/.*").mock(
        return_value=httpx.Response(200, text=fixture_text("oracle.json")))
    ctx = ctx_for(tmp_path, url="https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions")
    jobs = await build_adapter(ctx).fetch()
    calls = [str(c.request.url) for c in respx.calls]
    assert any("location=Germany" in u for u in calls) and any("location=Portugal" in u for u in calls)
    job = next(j for j in jobs if j.native_id == "9001")
    assert job.url.endswith("/sites/CX_1/job/9001") and job.posted_precision == "date"
    assert next(j for j in jobs if j.native_id == "9002").locations == ["Berlin, Germany", "Hamburg, Germany"]
    await ctx.http.aclose()


@respx.mock
async def test_eightfold(tmp_path):
    respx.get("https://acme.eightfold.ai/api/pcsx/search").mock(
        return_value=httpx.Response(200, text=fixture_text("eightfold.json")))
    ctx = ctx_for(tmp_path, url="https://acme.eightfold.ai/careers?domain=acme.com")
    jobs = await build_adapter(ctx).fetch()
    assert jobs[0].title == "UX Researcher"
    assert jobs[0].url == "https://acme.eightfold.ai/careers/job/555"
    assert jobs[0].posted_precision == "date"
    await ctx.http.aclose()


@respx.mock
async def test_eightfold_falls_back_to_legacy(tmp_path):
    respx.get("https://acme.eightfold.ai/api/pcsx/search").mock(return_value=httpx.Response(403))
    respx.get("https://acme.eightfold.ai/api/apply/v2/jobs").mock(return_value=httpx.Response(200, json={
        "count": 1, "positions": [{"id": 1, "name": "Product Manager", "location": "Berlin, Germany",
                                   "t_create": 1790294400}]}))
    ctx = ctx_for(tmp_path, url="https://acme.eightfold.ai/careers")
    jobs = await build_adapter(ctx).fetch()
    assert jobs[0].locations == ["Berlin, Germany"]
    await ctx.http.aclose()


@respx.mock
async def test_greenhouse(tmp_path):
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, text=fixture_text("greenhouse.json")))
    ctx = ctx_for(tmp_path, url="https://boards.greenhouse.io/acme")
    jobs = await build_adapter(ctx).fetch()
    assert jobs[0].description == "Own the roadmap"
    assert jobs[0].posted_at.day == 25
    await ctx.http.aclose()


@respx.mock
async def test_linkedin(tmp_path):
    route = respx.get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search").mock(
        return_value=httpx.Response(200, text=fixture_text("linkedin.html")))
    ctx = ctx_for(tmp_path, type="linkedin", company_names=["Acme Corp"], locations=["Germany"], max_pages=1)
    jobs = await build_adapter(ctx).fetch()
    assert [j.company for j in jobs] == ["Acme Corp"]  # 'Other Co' filtered out
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/4000000001/"
    assert "f_TPR" in str(route.calls[0].request.url)
    await ctx.http.aclose()


@respx.mock
async def test_successfactors_legacy(tmp_path):
    respx.get(url__startswith="https://career5.successfactors.eu/career").mock(
        return_value=httpx.Response(200, text=fixture_text("sf_legacy.xml")))
    ctx = ctx_for(tmp_path, url="https://career5.successfactors.eu/career?company=acmecorp")
    jobs = await build_adapter(ctx).fetch()
    assert jobs[0].title == "Product Owner DACH"
    assert jobs[0].locations == ["Lisbon, Portugal"]
    assert "career_job_req_id=123" in jobs[0].url
    await ctx.http.aclose()


@respx.mock
async def test_rss(tmp_path):
    respx.get("https://careers.example.test/rss").mock(return_value=httpx.Response(200, text=fixture_text("rss.xml")))
    ctx = ctx_for(tmp_path, type="rss", url="https://careers.example.test/rss", location_tag="category")
    jobs = await build_adapter(ctx).fetch()
    assert jobs[0].url == "https://careers.example.test/job/1"
    assert jobs[0].locations == ["Berlin"] and jobs[0].description == "Analyse funnels"
    await ctx.http.aclose()


@respx.mock
async def test_jsonld_sitemap(tmp_path):
    respx.get("https://recruiter.example.test/sitemap.xml").mock(
        return_value=httpx.Response(200, text=fixture_text("sitemap.xml")))
    respx.get("https://recruiter.example.test/jobs/ux-manager").mock(
        return_value=httpx.Response(200, text=fixture_text("jsonld.html")))
    ctx = ctx_for(tmp_path, type="jsonld_sitemap", url="https://recruiter.example.test/sitemap.xml",
                  url_pattern="/jobs/")
    jobs = await build_adapter(ctx).fetch()
    assert len(jobs) == 1  # old URL skipped, /about does not match
    assert jobs[0].title == "UX Research Manager"
    assert jobs[0].locations == ["Berlin, ES"]
    await ctx.http.aclose()


@respx.mock
async def test_json_api_graphql(tmp_path):
    route = respx.post("https://api.example.test/graphql").mock(
        return_value=httpx.Response(200, text=fixture_text("json_api.json")))
    ctx = ctx_for(
        tmp_path, type="json_api", url="https://api.example.test/graphql", method="POST",
        body={"variables": {"page": {"size": "{limit}", "number": "{page}"}, "q": "{query}"}},
        items="data.roles.items",
        fields={"id": "roleId", "title": "jobTitle", "url_template": "https://jobs.example.test/{roleId}",
                "location": "locations[].join(', ', [city || '', country || ''])"},
        pagination={"type": "page", "start": 0, "limit": 100},
    )
    jobs = await build_adapter(ctx).fetch()
    sent = json.loads(route.calls[0].request.content)
    assert sent["variables"]["page"] == {"size": 100, "number": 0}  # types preserved
    assert jobs[0].url == "https://jobs.example.test/A1"
    assert jobs[0].locations == ["Berlin, Germany"]
    assert jobs[1].locations == ["United States"]
    await ctx.http.aclose()


@respx.mock
async def test_page_monitor(tmp_path):
    route = respx.get("https://fo.example.test/careers").mock(
        return_value=httpx.Response(200, text=fixture_text("careers_page.html")))
    ctx = ctx_for(tmp_path, type="page_monitor", url="https://fo.example.test/careers", link_pattern="/careers/.+")
    assert await build_adapter(ctx).fetch() == []  # first run = baseline
    route.mock(return_value=httpx.Response(200, text=fixture_text("careers_page.html").replace(
        "</main>", '<a href="/careers/ux-analyst">UX Analyst</a></main>')))
    jobs = await build_adapter(ctx).fetch()
    assert [j.title for j in jobs] == ["UX Analyst"]
    await ctx.http.aclose()


@respx.mock
async def test_brassring(tmp_path):
    base = "https://careers.example.test"
    respx.get(f"{base}/TGnewUI/Search/Home/Home").mock(
        return_value=httpx.Response(200, text=fixture_text("brassring_home.html")))
    first = respx.post(f"{base}/TgNewUI/Search/Ajax/PowerSearchJobs").mock(
        return_value=httpx.Response(200, text=fixture_text("brassring_page1.json")))
    respx.post(f"{base}/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs").mock(
        return_value=httpx.Response(200, text=fixture_text("brassring_page2.json")))
    url = f"{base}/TGnewUI/Search/Home/Home?partnerid=111&siteid=222"
    assert detect_url(url)[0] == "brassring"
    ctx = ctx_for(tmp_path, url=url)
    jobs = await build_adapter(ctx).fetch()
    sent = json.loads(first.calls[0].request.content)
    assert sent["encryptedSessionValue"] == "enc-123" and sent["SortType"] == "LastUpdated"
    assert first.calls[0].request.headers["RFT"] == "tok-abc"
    assert [j.title for j in jobs] == ["Product Manager", "Data Analyst", "Old role"]
    assert jobs[0].locations == ["Berlin, Germany"] and jobs[0].posted_precision == "date"
    assert jobs[0].description == "Own the roadmap" and jobs[0].url.endswith("jobid=1")
    await ctx.http.aclose()


@respx.mock
async def test_teamtailor_custom_domain(tmp_path):
    respx.get("https://careers.acme.test/jobs.rss").mock(
        return_value=httpx.Response(200, text=fixture_text("teamtailor.rss")))
    ctx = ctx_for(tmp_path, type="teamtailor", url="https://careers.acme.test/jobs")
    jobs = await build_adapter(ctx).fetch()
    assert jobs[0].url == "https://careers.acme.test/jobs/123-product-manager"
    assert jobs[0].locations == ["Berlin, Germany"] and jobs[0].description == "Own the roadmap"
    assert jobs[0].posted_at.day == 25
    await ctx.http.aclose()


@respx.mock
async def test_jsonld_sitemap_cdata_and_loose_json(tmp_path):
    respx.get("https://recruiter.example.test/sitemap.xml").mock(
        return_value=httpx.Response(200, text=fixture_text("sitemap_cdata.xml")))
    respx.get("https://recruiter.example.test/vacancy/wealth-planner_R7").mock(
        return_value=httpx.Response(200, text=fixture_text("jsonld_loose.html")))
    ctx = ctx_for(tmp_path, type="jsonld_sitemap", url="https://recruiter.example.test/sitemap.xml",
                  url_pattern="/vacancy/")
    jobs = await build_adapter(ctx).fetch()
    assert [j.title for j in jobs] == ["Wealth Planner"]  # CDATA read, old URL skipped
    assert jobs[0].description == "Advise families on succession"  # raw newline in the JSON string
    assert jobs[0].locations == ["Lisbon, PT"] and jobs[0].posted_at.day == 25
    await ctx.http.aclose()


@respx.mock
async def test_rss_cdata(tmp_path):
    respx.get("https://recruiter.example.test/vacancies/feed/").mock(
        return_value=httpx.Response(200, text=fixture_text("rss_cdata.xml")))
    ctx = ctx_for(tmp_path, type="rss", url="https://recruiter.example.test/vacancies/feed/",
                  location_regex="Location:\\s*([^<\\n]+)")
    jobs = await build_adapter(ctx).fetch()
    assert jobs[0].title == "Head of Product & Growth"
    assert jobs[0].url == "https://recruiter.example.test/vacancies/head-of-product/"
    assert jobs[0].locations == ["Lisbon"] and jobs[0].description == "Location: Lisbon Lead the product team"
    await ctx.http.aclose()


@respx.mock
async def test_html_list_urls_404_and_date_regex(tmp_path, monkeypatch):
    base = "https://recruiter.example.test/jobs"
    listing = fixture_text("recruiter_list.html")
    second = listing.replace("/job/101", "/job/103").replace("Product Manager", "UX Researcher")
    respx.get(f"{base}/finance?page=0").mock(return_value=httpx.Response(200, text=listing))
    respx.get(f"{base}/finance?page=1").mock(return_value=httpx.Response(404))  # past the last page
    respx.get(f"{base}/ux-research?page=0").mock(return_value=httpx.Response(200, text=second))
    respx.get(f"{base}/ux-research?page=1").mock(return_value=httpx.Response(404))
    respx.get(f"{base}/nothing-here?page=0").mock(return_value=httpx.Response(404))  # search without results
    ctx = ctx_for(tmp_path, type="html_list", urls=[f"{base}/finance?page={{page}}", f"{base}/{{query_slug}}?page={{page}}"],
                  queries=["UX research", "nothing here"], item="li.job", title="h3 a", link="h3 a",
                  location="p.meta", location_regex=r"·\s*(.+)$", date="p.meta", date_regex=r"(\d{2}/\d{2}/\d{4})",
                  pagination={"start": 0, "max_pages": 3}, delay_seconds=2)
    pauses = []

    async def fake_sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr("jobfinder.sources.generic.html_list.asyncio.sleep", fake_sleep)
    jobs = await build_adapter(ctx).fetch()
    assert [j.title for j in jobs] == ["Product Manager", "Data Analyst", "UX Researcher"]
    assert pauses == [2] * 4  # between the 5 listing requests
    assert jobs[0].url == "https://recruiter.example.test/job/101" and jobs[0].locations == ["Berlin, Germany"]
    assert (jobs[0].posted_at.day, jobs[0].posted_at.month, jobs[0].posted_precision) == (25, 9, "date")
    await ctx.http.aclose()


@respx.mock
async def test_html_list_404_on_first_page_fails(tmp_path):
    from jobfinder.sources.http import HttpError

    respx.get("https://recruiter.example.test/jobs").mock(return_value=httpx.Response(404))
    ctx = ctx_for(tmp_path, type="html_list", url="https://recruiter.example.test/jobs", item="li.job")
    with pytest.raises(HttpError):
        await build_adapter(ctx).fetch()
    await ctx.http.aclose()


@respx.mock
async def test_html_list_detail_jsonld(tmp_path):
    listing = fixture_text("recruiter_list.html").replace("Published 25/09/2026 · Berlin, Germany", "")
    respx.get("https://recruiter.example.test/jobs").mock(return_value=httpx.Response(200, text=listing))
    respx.get("https://recruiter.example.test/job/101").mock(
        return_value=httpx.Response(200, text=fixture_text("jsonld_loose.html")))
    ctx = ctx_for(tmp_path, type="html_list", url="https://recruiter.example.test/jobs", item="li.job",
                  title="h3 a", location="p.meta", detail={"jsonld": True})
    adapter = build_adapter(ctx)
    job = (await adapter.fetch())[0]
    assert job.posted_at is None and job.locations == []
    await adapter.enrich(job)
    assert job.description == "Advise families on succession"
    assert job.posted_at.day == 25 and job.posted_precision == "date"
    assert job.locations == ["Lisbon, PT"]
    await ctx.http.aclose()


@respx.mock
async def test_jsonld_sitemap_skips_a_failing_page(tmp_path):
    sitemap = fixture_text("sitemap_cdata.xml").replace("2026-01-01", "2026-09-26")  # both URLs fresh
    respx.get("https://recruiter.example.test/sitemap.xml").mock(return_value=httpx.Response(200, text=sitemap))
    respx.get("https://recruiter.example.test/vacancy/wealth-planner_R7").mock(
        return_value=httpx.Response(200, text=fixture_text("jsonld_loose.html")))
    broken = respx.get("https://recruiter.example.test/vacancy/old-role_R1").mock(return_value=httpx.Response(404))
    ctx = ctx_for(tmp_path, type="jsonld_sitemap", url="https://recruiter.example.test/sitemap.xml",
                  url_pattern="/vacancy/")
    assert [j.title for j in await build_adapter(ctx).fetch()] == ["Wealth Planner"]
    respx.get("https://recruiter.example.test/vacancy/wealth-planner_R7").mock(return_value=httpx.Response(404))
    with pytest.raises(Exception, match="HTTP 404"):  # every page failing is still an error
        await build_adapter(ctx).fetch()
    assert broken.called
    await ctx.http.aclose()
