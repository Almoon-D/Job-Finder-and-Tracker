"""eFinancialCareers: the JSON API behind its public job search. No login.

    - name: eFinancialCareers
      type: efinancialcareers
      group: boards
      countries: [ES, CH]     # default: the countries of your configured locations
      queries: [investor relations]   # optional extra searches inside those countries

For each country it reads every job, newest first, until the jobs are older than the
group's age window (the search only sorts by date when there is no keyword). Keyword
queries are sorted by relevance, so only their first page is read. The coverage search
terms run worldwide. Listings include the full description, so no detail request is needed.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from ...dates import parse_date
from ...matching.location import resolve_country
from ...models import Job
from ..base import Adapter, register
from ..util import html_to_text

API = "https://job-search-api.efinancialcareers.com/v1/efc/jobs/search"
SITE = "https://www.efinancialcareers.com"
PAGE_SIZE = 100


@register
class EFinancialCareers(Adapter):
    type_name = "efinancialcareers"
    supports_search = True

    def _countries(self) -> list[str]:
        wanted = self.params.get("countries") or self.ctx.locations.countries()
        return [c for c in (resolve_country(str(x)) for x in wanted) if c]

    async def _page(self, page: int, query: str, country: str | None, size: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": query, "page": page, "pageSize": size, "language": "en"}
        if country:
            params.update(countryCode2=country, locationPrecision="Country")
        if not query:
            params["sortBy"] = "POSTED_DATE"
        data = await self.http.get_json(API, params=params)
        return data.get("data") or []

    def _to_job(self, item: dict[str, Any]) -> Job | None:
        path = item.get("detailsPageUrl") or ""
        if not item.get("id") or not item.get("title") or not path:
            return None
        posted, precision = parse_date(item.get("postedDate"), self.ctx.now)
        loc = (item.get("jobLocation") or {}).get("displayName")
        return self.job(item["id"], item["title"], SITE + path if path.startswith("/") else path,
                        company=item.get("companyName") or item.get("clientBrandName") or None,
                        locations=[loc] if loc else [], posted_at=posted, posted_precision=precision,
                        description=html_to_text(item.get("description") or item.get("summary") or ""))

    async def _newest(self, country: str) -> list[Job]:
        """Every job of a country, newest first, until the age window is left."""
        oldest = self.ctx.now - timedelta(days=self.ctx.max_age_days + 0.5)
        jobs: list[Job] = []
        for page in range(1, self.source.max_pages + 1):
            items = await self._page(page, "", country, PAGE_SIZE)
            batch = [j for j in (self._to_job(i) for i in items) if j]
            jobs += batch
            if len(items) < PAGE_SIZE or any(j.posted_at and j.posted_at < oldest for j in batch):
                break
            await asyncio.sleep(0.5)
        return jobs

    async def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        countries = self._countries()
        for country in countries:
            jobs += await self._newest(country)
            for q in self.search_terms():
                jobs += [j for j in map(self._to_job, await self._page(1, q, country, 50)) if j]
        for q in self.coverage_terms():
            jobs += [j for j in map(self._to_job, await self._page(1, q, None, 50)) if j]
        return list({j.key: j for j in jobs}.values())
