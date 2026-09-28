"""jobup.ch and jobs.ch (JobCloud): the public JSON search API behind both sites. No login.

    - name: jobup.ch
      type: jobcloud
      site: jobup.ch                 # or jobs.ch
      group: boards
      queries: [private banker, gestionnaire de fortune]
      locations: [Geneva, Lausanne]  # default: your configured Swiss cities
      max_pages: 2                   # 20 jobs per page, newest first

Results are sorted by date; paging stops as soon as a page reaches jobs older than the
group's age window. The API sits behind CloudFront, which blocks bursts, so requests are
spaced out. Without ``queries`` it reads the whole listing of each place (can be large).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from ...dates import parse_date
from ...models import Job
from ..base import Adapter, AdapterError, register
from ..generic.jsonld import job_postings
from ..util import html_to_text

ROWS = 20  # API maximum
PAUSE = 1.5
SITES = ("jobup.ch", "jobs.ch")


@register
class JobCloud(Adapter):
    type_name = "jobcloud"
    supports_search = True

    @property
    def site(self) -> str:
        site = str(self.params.get("site") or "jobup.ch").removeprefix("www.")
        if site not in SITES:
            raise AdapterError(f"jobcloud: site must be one of {', '.join(SITES)}")
        return site

    def _places(self) -> list[str]:
        if self.params.get("locations"):
            return [str(x) for x in self.params["locations"]]
        swiss = [t.label for t in self.ctx.locations.targets if t.label and t.country == "CH"]
        return swiss or [""]

    def _link(self, doc: dict[str, Any]) -> str:
        links = doc.get("_links") or {}
        for lang in (self.params.get("language") or "fr", "en", "fr", "de"):
            href = (links.get(f"detail_{lang}") or {}).get("href")
            if href:
                return href
        return f"https://www.{self.site}/en/jobs/detail/{doc.get('job_id')}/"

    def _to_job(self, doc: dict[str, Any]) -> Job | None:
        if not doc.get("job_id") or not doc.get("title"):
            return None
        posted, precision = parse_date(doc.get("publication_date"), self.ctx.now)
        place = doc.get("place")
        return self.job(doc["job_id"], doc["title"], self._link(doc),
                        company=doc.get("company_name") if doc.get("company_visible", True) else None,
                        locations=[f"{place}, Switzerland"] if place else [],
                        posted_at=posted, posted_precision=precision, description=doc.get("preview") or "")

    async def _search(self, query: str, place: str, pages: int) -> list[Job]:
        oldest = self.ctx.now - timedelta(days=self.ctx.max_age_days + 0.5)
        jobs: list[Job] = []
        for page in range(1, pages + 1):
            params = {"query": query, "location": place, "sort-by": "date", "page": page, "rows": ROWS}
            data = await self.http.get_json(f"https://www.{self.site}/api/v1/public/search", params=params)
            await asyncio.sleep(PAUSE)
            batch = [j for j in map(self._to_job, data.get("documents") or []) if j]
            jobs += batch
            if len(batch) < ROWS or page >= int(data.get("num_pages") or 0):
                break
            if any(j.posted_at and j.posted_at < oldest for j in batch):
                break
        return jobs

    async def fetch(self) -> list[Job]:
        pages = self.source.max_pages if "max_pages" in self.source.model_fields_set else 2
        jobs: list[Job] = []
        for place in self._places():
            for q in self.search_terms() or [""]:
                jobs += await self._search(q, place, pages)
        for q in self.coverage_terms():
            jobs += await self._search(q, "", 1)
        return list({j.key: j for j in jobs}.values())

    async def enrich(self, job: Job) -> None:
        postings = job_postings(await self.http.get_text(job.url))
        if postings:
            job.description = html_to_text(postings[0].get("description")) or job.description
