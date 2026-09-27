"""ATS vendors with simple public JSON job-board APIs.

Each of them returns all open jobs of a company in one or a few requests, so the
location filter is applied client-side by the pipeline.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from ...dates import parse_date
from ...models import Job
from ..base import Adapter, register
from ..util import as_list, dig, html_to_text


def _first_seg(url: str) -> str | None:
    segs = [s for s in urlsplit(url).path.split("/") if s]
    return segs[0] if segs else None


@register
class Greenhouse(Adapter):
    type_name = "greenhouse"

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        parts = urlsplit(url)
        if parts.netloc in ("boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io",
                            "job-boards.eu.greenhouse.io"):
            board = _first_seg(url)
            if board and board != "embed":
                return {"board": board}
            m = re.search(r"for=([\w-]+)", parts.query)
            if m:
                return {"board": m.group(1)}
        if parts.netloc == "boards-api.greenhouse.io":
            m = re.search(r"/boards/([\w-]+)", parts.path)
            if m:
                return {"board": m.group(1)}
        return None

    async def fetch(self) -> list[Job]:
        data = await self.http.get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{self.params['board']}/jobs", params={"content": "true"}
        )
        out = []
        for j in data.get("jobs") or []:
            posted, precision = parse_date(j.get("first_published") or j.get("updated_at"), self.ctx.now)
            locs = [dig(j, "location", "name")] + [o.get("name") for o in j.get("offices") or []]
            out.append(self.job(j.get("id"), j.get("title", ""), j.get("absolute_url", ""),
                                locations=[x for x in dict.fromkeys(locs) if x], posted_at=posted,
                                posted_precision=precision, description=html_to_text(j.get("content"))))
        return out


@register
class Lever(Adapter):
    type_name = "lever"

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        parts = urlsplit(url)
        if parts.netloc in ("jobs.lever.co", "jobs.eu.lever.co"):
            company = _first_seg(url)
            if company:
                api = "api.eu.lever.co" if ".eu." in parts.netloc else "api.lever.co"
                return {"company": company, "api": api}
        return None

    async def fetch(self) -> list[Job]:
        api = self.params.get("api", "api.lever.co")
        data = await self.http.get_json(f"https://{api}/v0/postings/{self.params['company']}", params={"mode": "json"})
        out = []
        for j in data or []:
            posted, precision = parse_date(j.get("createdAt"), self.ctx.now)
            cats = j.get("categories") or {}
            locs = as_list(cats.get("allLocations")) or [cats.get("location")]
            desc = " ".join([j.get("descriptionPlain") or "", j.get("additionalPlain") or ""])
            out.append(self.job(j.get("id"), j.get("text", ""), j.get("hostedUrl", ""),
                                locations=[x for x in locs if x], posted_at=posted, posted_precision=precision,
                                description=desc.strip()))
        return out


@register
class SmartRecruiters(Adapter):
    type_name = "smartrecruiters"

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        parts = urlsplit(url)
        if parts.netloc in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
            company = _first_seg(url)
            if company:
                return {"company": company}
        return None

    async def fetch(self) -> list[Job]:
        out: list[Job] = []
        company = self.params["company"]
        # With coverage matching we need every posting (a role abroad may cover your market);
        # otherwise let the API filter by country.
        coverage = self.ctx.config.coverage
        use_country_filter = not (coverage.enabled and coverage.text_any)
        countries = [c.lower() for c in self.ctx.locations.countries()] if use_country_filter else []
        countries = countries or [None]
        for country in countries:
            for page in range(self.source.max_pages):
                params: dict[str, Any] = {"limit": 100, "offset": page * 100}
                if country:
                    params["country"] = country
                data = await self.http.get_json(f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                                                params=params)
                content = data.get("content") or []
                for j in content:
                    posted, precision = parse_date(j.get("releasedDate"), self.ctx.now)
                    loc = j.get("location") or {}
                    loc_text = loc.get("fullLocation") or ", ".join(
                        x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
                    if loc.get("remote"):
                        loc_text = f"Remote, {loc_text}"
                    job = self.job(j.get("id"), j.get("name", ""),
                                   f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}",
                                   locations=[loc_text], posted_at=posted, posted_precision=precision)
                    job.extra["ref"] = j.get("ref")
                    out.append(job)
                if len(content) < 100 or (page + 1) * 100 >= int(data.get("totalFound") or 0):
                    break
        return out

    async def enrich(self, job: Job) -> None:
        ref = job.extra.get("ref")
        if not ref:
            return
        data = await self.http.get_json(ref)
        sections = dig(data, "jobAd", "sections", default={}) or {}
        job.description = html_to_text(" ".join((s or {}).get("text", "") for s in sections.values()))


@register
class Ashby(Adapter):
    type_name = "ashby"

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        if urlsplit(url).netloc == "jobs.ashbyhq.com":
            board = _first_seg(url)
            if board:
                return {"board": board}
        return None

    async def fetch(self) -> list[Job]:
        data = await self.http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{self.params['board']}")
        out = []
        for j in data.get("jobs") or []:
            posted, precision = parse_date(j.get("publishedAt"), self.ctx.now)
            locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
            out.append(self.job(j.get("id"), j.get("title", ""), j.get("jobUrl", ""),
                                locations=[x for x in locs if x], posted_at=posted, posted_precision=precision,
                                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))))
        return out


@register
class Workable(Adapter):
    type_name = "workable"

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        parts = urlsplit(url)
        if parts.netloc in ("apply.workable.com", "jobs.workable.com"):
            account = _first_seg(url)
            if account and account != "api":
                return {"account": account}
        return None

    async def fetch(self) -> list[Job]:
        account = self.params["account"]
        data = await self.http.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{account}",
                                        params={"details": "true"})
        out = []
        for j in data.get("jobs") or []:
            posted, precision = parse_date(j.get("published_on") or j.get("created_at"), self.ctx.now)
            locs = [", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
                    for loc in j.get("locations") or []]
            if not locs:
                locs = [", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)]
            out.append(self.job(j.get("shortcode"), j.get("title", ""),
                                j.get("url") or j.get("application_url") or "",
                                locations=[x for x in locs if x], posted_at=posted, posted_precision=precision,
                                description=html_to_text(j.get("description"))))
        return out


@register
class Recruitee(Adapter):
    type_name = "recruitee"

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        host = urlsplit(url).netloc
        if host.endswith(".recruitee.com"):
            return {"company": host.split(".")[0]}
        return None

    async def fetch(self) -> list[Job]:
        data = await self.http.get_json(f"https://{self.params['company']}.recruitee.com/api/offers/")
        out = []
        for j in data.get("offers") or []:
            posted, precision = parse_date(j.get("published_at") or j.get("created_at"), self.ctx.now)
            locs = [loc.get("name") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
                    for loc in j.get("locations") or []] or [j.get("location")]
            out.append(self.job(j.get("id"), j.get("title", ""), j.get("careers_url", ""),
                                locations=[x for x in locs if x], posted_at=posted, posted_precision=precision,
                                description=html_to_text(j.get("description"))))
        return out


@register
class Teamtailor(Adapter):
    """Teamtailor career sites, on *.teamtailor.com or on a custom domain (set `type: teamtailor`)."""

    type_name = "teamtailor"

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        host = urlsplit(url).netloc
        if host.endswith(".teamtailor.com"):
            return {"host": host}
        return None

    async def fetch(self) -> list[Job]:
        from xml.etree import ElementTree

        host = self.params.get("host") or urlsplit(self.params.get("url", "")).netloc
        xml = await self.http.get_text(f"https://{host}/jobs.rss")
        root = ElementTree.fromstring(xml.encode("utf-8"))
        tt = "{https://teamtailor.com/locations}"
        out = []
        for item in root.iter("item"):
            link = (item.findtext("link") or "").strip()
            if not link:
                continue
            posted, precision = parse_date(item.findtext("pubDate"), self.ctx.now)
            locs = []
            for loc in item.iter(f"{tt}location"):
                parts = [loc.findtext(f"{tt}{k}") for k in ("city", "country")]
                text = ", ".join(p.strip() for p in parts if p and p.strip())
                if text:
                    locs.append(text)
            native = (item.findtext("guid") or link).strip()
            out.append(self.job(native, (item.findtext("title") or "").strip(), link,
                                locations=list(dict.fromkeys(locs)), posted_at=posted, posted_precision=precision,
                                description=html_to_text(item.findtext("description"))))
        return out


@register
class Personio(Adapter):
    type_name = "personio"

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        host = urlsplit(url).netloc
        m = re.match(r"^([\w-]+)\.jobs\.personio\.(de|com)$", host)
        if m:
            return {"host": host}
        return None

    async def fetch(self) -> list[Job]:
        from selectolax.parser import HTMLParser

        xml = await self.http.get_text(f"https://{self.params['host']}/xml")
        tree = HTMLParser(xml)
        out = []
        for pos in tree.css("position"):
            def txt(tag: str, node=pos) -> str:
                n = node.css_first(tag)
                return n.text(strip=True) if n else ""

            pid = txt("id")
            posted, precision = parse_date(txt("createdat"), self.ctx.now)
            locs = [txt("office")] + [n.text(strip=True) for n in pos.css("additionaloffices office")]
            desc = " ".join(n.text(separator=" ") for n in pos.css("jobdescription value"))
            out.append(self.job(pid, txt("name"), f"https://{self.params['host']}/job/{pid}",
                                locations=[x for x in dict.fromkeys(locs) if x], posted_at=posted,
                                posted_precision=precision, description=html_to_text(desc)))
        return out
