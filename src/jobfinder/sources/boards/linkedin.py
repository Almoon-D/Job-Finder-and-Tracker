"""LinkedIn public (guest) job search. No login, no account involved.

    - name: LinkedIn searches
      type: linkedin
      group: boards
      queries: ["product manager", "data analyst"]
      # locations default to the configured cities/countries; override with:
      locations: ["Berlin, Germany", "Lisbon, Portugal"]
      company_ids: [1068]          # optional: only jobs of these companies (f_C)
      company_names: ["Example Bank"]  # optional: search by company name and keep only its jobs
      max_pages: 3                 # 10 jobs per page

LinkedIn rate-limits datacenter IPs (HTTP 429/999). Keep the number of queries
small; never use it in a group polled every few minutes.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from selectolax.parser import HTMLParser

from ...dates import parse_date
from ...matching.location import country_name
from ...models import Job, normalize_text
from ..base import Adapter, register
from ..util import html_to_text

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
_URN = re.compile(r"jobPosting:(\d+)")
_VIEW_ID = re.compile(r"/jobs/view/(?:[^/?]*-)?(\d+)")


def parse_cards(html: str) -> list[dict[str, Any]]:
    out = []
    for card in HTMLParser(html).css("div.base-card, div.job-search-card, li"):
        urn = card.attributes.get("data-entity-urn") or ""
        link = card.css_first("a.base-card__full-link") or card.css_first("a")
        href = (link.attributes.get("href") if link else "") or ""
        m = _URN.search(urn) or _VIEW_ID.search(href)
        if not m:
            continue

        def txt(sel: str, node=card) -> str:
            n = node.css_first(sel)
            return " ".join(n.text(separator=" ").split()) if n else ""

        date_node = card.css_first("time")
        out.append({
            "id": m.group(1),
            "title": txt("h3.base-search-card__title") or txt(".sr-only"),
            "company": txt("h4.base-search-card__subtitle"),
            "location": txt("span.job-search-card__location"),
            "date": date_node.attributes.get("datetime") if date_node else None,
        })
    return list({c["id"]: c for c in out}.values())


@register
class LinkedIn(Adapter):
    type_name = "linkedin"
    supports_search = True

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        return None  # always configured explicitly with type: linkedin

    def _locations(self) -> list[str]:
        if self.params.get("locations"):
            return list(self.params["locations"])
        m = self.ctx.locations
        return m.cities() + [country_name(c) for c in m.countries() if c not in self._countries_with_cities()]

    def _countries_with_cities(self) -> set[str]:
        return {t.country for t in self.ctx.locations.targets if t.label and t.country}

    async def _search(self, keywords: str, location: str | None, pages: int) -> list[Job]:
        seconds = int(max(self.ctx.max_age_days, 1) * 86400)
        jobs: list[Job] = []
        for page in range(pages):
            params: dict[str, Any] = {"keywords": keywords, "start": page * 10, "f_TPR": f"r{seconds}"}
            if location:
                params["location"] = location
            if self.params.get("company_ids"):
                params["f_C"] = ",".join(str(c) for c in self.params["company_ids"])
            if self.params.get("experience_levels"):
                params["f_E"] = ",".join(str(c) for c in self.params["experience_levels"])
            html = await self.http.get_text(SEARCH, params=params)
            cards = parse_cards(html)
            for c in cards:
                posted, precision = parse_date(c["date"], self.ctx.now)
                jobs.append(self.job(c["id"], c["title"], f"https://www.linkedin.com/jobs/view/{c['id']}/",
                                     company=c["company"] or None, locations=[c["location"]] if c["location"] else [],
                                     posted_at=posted, posted_precision=precision))
            if len(cards) < 10:
                break
            await asyncio.sleep(1.5)  # be gentle
        return jobs

    async def fetch(self) -> list[Job]:
        pages = min(self.source.max_pages, int(self.params.get("max_pages", 3)))
        names = [str(n) for n in self.params.get("company_names") or []]
        queries = self.search_terms() or ([""] if self.params.get("company_ids") else [])
        jobs: list[Job] = []
        for q in queries:
            for loc in self._locations() or [None]:
                jobs += await self._search(q, loc, pages)
        for name in names:
            wanted = normalize_text(name)
            for loc in self._locations() or [None]:
                found = await self._search(name, loc, pages)
                jobs += [j for j in found if wanted and wanted in normalize_text(j.company)]
        if not names and not self.params.get("company_ids"):  # company-filtered sources skip coverage
            for q in self.coverage_terms():
                jobs += await self._search(q, self.params.get("coverage_location", "Europe"), 1)
        return list({j.key: j for j in jobs}.values())

    async def enrich(self, job: Job) -> None:
        html = await self.http.get_text(DETAIL.format(id=job.native_id))
        node = HTMLParser(html).css_first("div.show-more-less-html__markup, div.description__text")
        job.description = html_to_text(node.html if node else "")
