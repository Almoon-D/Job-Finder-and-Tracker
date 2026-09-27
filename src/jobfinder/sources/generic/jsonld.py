"""schema.org JobPosting (JSON-LD) extraction: from a sitemap or from listing pages.

Most career sites and recruiters publish JobPosting JSON-LD so Google Jobs can
index them. This adapter needs no site-specific code::

    - name: Example Recruiter
      type: jsonld_sitemap
      url: https://recruiter.example/sitemap.xml   # sitemap or sitemap index
      url_pattern: "/jobs?/"                        # regex that job URLs match
      max_urls: 60                                  # pages fetched per run (newest first)
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from selectolax.parser import HTMLParser

from ...dates import parse_date
from ...models import Job
from ..base import Adapter, AdapterError, register
from ..util import as_list, html_to_text


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def job_postings(html: str) -> list[dict[str, Any]]:
    out = []
    for node in HTMLParser(html).css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        for obj in _walk(data):
            types = as_list(obj.get("@type"))
            if "JobPosting" in types:
                out.append(obj)
    return out


def posting_locations(p: dict[str, Any]) -> list[str]:
    locs = []
    for loc in as_list(p.get("jobLocation")):
        addr = loc.get("address") if isinstance(loc, dict) else None
        if isinstance(addr, dict):
            country = addr.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            parts = [addr.get("addressLocality"), addr.get("addressRegion"), country]
            locs.append(", ".join(str(x) for x in parts if x))
        elif isinstance(addr, str):
            locs.append(addr)
    if str(p.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        req = [r.get("name") if isinstance(r, dict) else r for r in as_list(p.get("applicantLocationRequirements"))]
        locs.append("Remote, " + ", ".join(str(r) for r in req if r))
    return [x for x in locs if x]


def posting_to_job(adapter: Adapter, p: dict[str, Any], page_url: str) -> Job:
    posted, precision = parse_date(p.get("datePosted"), adapter.ctx.now)
    org = p.get("hiringOrganization")
    company = org.get("name") if isinstance(org, dict) else None
    ident = p.get("identifier")
    native = ident.get("value") if isinstance(ident, dict) else ident
    url = p.get("url") or page_url
    return adapter.job(native or url, html_to_text(p.get("title") or ""), url, locations=posting_locations(p),
                       posted_at=posted, posted_precision=precision,
                       description=html_to_text(p.get("description")),
                       company=company if adapter.params.get("company_from_page") else None)


@register
class JsonLdSitemap(Adapter):
    type_name = "jsonld_sitemap"

    async def _sitemap_urls(self, url: str, depth: int = 0) -> list[tuple[str, str | None]]:
        xml = await self.http.get_text(url)
        tree = HTMLParser(xml)
        out: list[tuple[str, str | None]] = []
        if tree.css_first("sitemapindex") and depth < 2:
            pattern = self.params.get("sitemap_pattern")
            for sm in tree.css("sitemap"):
                loc = sm.css_first("loc")
                if loc and (not pattern or re.search(pattern, loc.text(strip=True))):
                    out += await self._sitemap_urls(loc.text(strip=True), depth + 1)
            return out
        for u in tree.css("url"):
            loc, lastmod = u.css_first("loc"), u.css_first("lastmod")
            if loc:
                out.append((loc.text(strip=True), lastmod.text(strip=True) if lastmod else None))
        return out

    async def fetch(self) -> list[Job]:
        if not self.params.get("url"):
            raise AdapterError("jsonld_sitemap needs 'url'")
        pattern = re.compile(self.params.get("url_pattern") or r".")
        max_urls = int(self.params.get("max_urls", 60))
        window = timedelta(days=self.ctx.max_age_days + 1)
        entries = [(u, m) for u, m in await self._sitemap_urls(self.params["url"]) if pattern.search(u)]
        fresh = []
        for u, lastmod in entries:
            dt, _ = parse_date(lastmod, self.ctx.now)
            if dt is None:
                # No lastmod: only visit URLs we have never seen before.
                if f"{self.source.key}:{u}" not in self.ctx.state.seen:
                    fresh.append((u, None))
            elif self.ctx.now - dt <= window:
                fresh.append((u, dt))
        fresh.sort(key=lambda e: e[1] or self.ctx.now, reverse=True)
        jobs = []
        for u, _ in fresh[:max_urls]:
            html = await self.http.get_text(u)
            for p in job_postings(html)[:1]:
                job = posting_to_job(self, p, u)
                job.native_id = u  # stable id = page URL
                jobs.append(job)
        return jobs


@register
class JsonLdPages(Adapter):
    """JobPosting JSON-LD embedded directly in one or more listing pages."""

    type_name = "jsonld_pages"

    async def fetch(self) -> list[Job]:
        urls = as_list(self.params.get("urls") or self.params.get("url"))
        if not urls:
            raise AdapterError("jsonld_pages needs 'url' or 'urls'")
        jobs = []
        for u in urls:
            html = await self.http.get_text(u)
            jobs += [posting_to_job(self, p, u) for p in job_postings(html)]
        return jobs
