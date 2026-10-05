"""SAP SuccessFactors career sites.

Two flavours exist:

* Legacy portal ``https://careerN.successfactors.eu/career?company=<id>`` – exposes
  an XML listing (``resultType=XML``). Auto-detected from the URL. Some tenants (Sabadell,
  Naturgy) answer an empty list unless the site locale is sent: set ``locale: es_ES``.
* Recruiting Marketing ("RMK") sites on a custom domain (e.g. careers.example.com,
  pages like /job/<slug>/<id>/) – configure ``type: successfactors`` and the site
  root as ``url``. The newest jobs are read from the search page and details from
  the schema.org microdata of each job page.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from selectolax.parser import HTMLParser

from ...dates import parse_date
from ...models import Job
from ..base import Adapter, register
from ..util import absolute, html_to_text

PAGE = 25
_JOB_ID = re.compile(r"/(\d+)/?$")


def _first(fields: dict[str, str], *labels: str) -> str | None:
    """The first non-empty filter value among these (lower-case) labels: tenants label them in their language."""
    return next((fields[x] for x in labels if fields.get(x)), None)


@register
class SuccessFactors(Adapter):
    type_name = "successfactors"
    supports_search = True

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        parts = urlsplit(url)
        if "successfactors" in parts.netloc:
            company = (parse_qs(parts.query).get("company") or [None])[0]
            if company:
                return {"mode": "legacy", "host": parts.netloc, "company": company}
        return None

    # ------------------------------------------------------------ legacy
    async def _legacy(self) -> list[Job]:
        p = self.params
        base = f"https://{p['host']}/career?company={p['company']}"
        locale = f"&rcm_site_locale={p['locale']}" if p.get("locale") else ""
        xml = await self.http.get_text(f"{base}&career_ns=job_listing_summary&resultType=XML{locale}")
        root = ElementTree.fromstring(xml.encode("utf-8"))
        jobs = []
        for node in root.iter("Job"):
            title, req = (node.findtext("JobTitle") or "").strip(), (node.findtext("ReqId") or "").strip()
            if not title or not req:
                continue
            fields = {
                (child.findtext("label") or "").strip().lower(): (child.findtext("value") or "").strip()
                for child in node if child.find("label") is not None
            }
            loc = ", ".join(x for x in (_first(fields, "city", "ciudad", "localidad"),
                                        _first(fields, "state", "región/provincia/estado", "provincia"),
                                        _first(fields, "country", "país", "pais")) if x)
            posted, precision = parse_date(node.findtext("Posted-Date"), self.ctx.now)
            jobs.append(self.job(req, html_to_text(title), f"{base}&career_ns=job_listing&career_job_req_id={req}",
                                 locations=[loc] if loc else [], posted_at=posted, posted_precision=precision,
                                 description=html_to_text(node.findtext("Job-Description") or "")))
        return jobs

    # --------------------------------------------------------------- RMK
    @property
    def root(self) -> str:
        parts = urlsplit(self.params["url"])
        return f"{parts.scheme}://{parts.netloc}"

    async def _rmk_search(self, query: str, pages: int) -> list[Job]:
        jobs: list[Job] = []
        locale = self.params.get("locale")
        page_size = 0
        for page in range(pages):
            params = {"q": query, "sortColumn": "referencedate", "sortDirection": "desc",
                      "startrow": page * (page_size or PAGE)}
            if locale:
                params["locale"] = locale
            html = await self.http.get_text(f"{self.root}/search/", params=params)
            tree = HTMLParser(html)
            rows = tree.css("tr.data-row")
            for row in rows:
                link = row.css_first("a.jobTitle-link")
                if not link:
                    continue
                href = absolute(self.root, link.attributes.get("href"))
                m = _JOB_ID.search(urlsplit(href).path)
                loc_node = row.css_first("span.jobLocation")
                date_node = row.css_first("span.jobDate")
                posted, precision = parse_date(date_node.text(strip=True) if date_node else None, self.ctx.now)
                jobs.append(self.job(m.group(1) if m else href, link.text(strip=True), href,
                                     locations=[" ".join(loc_node.text().split())] if loc_node else [],
                                     posted_at=posted, posted_precision=precision))
            page_size = page_size or len(rows)
            if not rows or len(rows) < page_size:
                break
        return jobs

    async def fetch(self) -> list[Job]:
        if self.params.get("mode") == "legacy":
            return await self._legacy()
        jobs = await self._rmk_search("", int(self.params.get("pages", 8))) if self.full_listing else []
        for q in self.search_terms():
            jobs += await self._rmk_search(q, 2)
        for q in self.coverage_terms():
            jobs += await self._rmk_search(q, 1)
        return list({j.key: j for j in jobs}.values())

    async def enrich(self, job: Job) -> None:
        if self.params.get("mode") == "legacy":
            return
        tree = HTMLParser(await self.http.get_text(job.url))
        desc = tree.css_first('[itemprop="description"]')
        job.description = html_to_text(desc.html if desc else "")
        date = tree.css_first('meta[itemprop="datePosted"]')
        if date and job.posted_at is None:
            job.posted_at, job.posted_precision = parse_date(date.attributes.get("content"), self.ctx.now)
            if job.posted_precision == "datetime":
                job.posted_precision = "date"
        parts = [tree.css_first(f'meta[itemprop="{k}"]') for k in ("addressLocality", "addressCountry")]
        loc = ", ".join(p.attributes.get("content") or "" for p in parts if p)
        if loc.strip(", "):
            job.locations = [loc]
