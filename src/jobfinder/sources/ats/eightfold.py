"""Eightfold.ai career sites (PCSX API, with fallback to the older /api/apply/v2 API)."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

from ...dates import parse_date
from ...matching.location import country_name
from ...models import Job
from ..base import Adapter, register
from ..http import HttpError
from ..util import html_to_text


@register
class Eightfold(Adapter):
    type_name = "eightfold"
    supports_search = True

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        parts = urlsplit(url)
        if parts.netloc.endswith(".eightfold.ai"):
            qs = parse_qs(parts.query)
            domain = (qs.get("domain") or [None])[0]
            if not domain:
                domain = parts.netloc.split(".")[0] + ".com"
            return {"host": parts.netloc, "domain": domain}
        return None

    @property
    def base(self) -> str:
        return f"https://{self.params['host']}"

    def _to_job(self, p: dict[str, Any]) -> Job:
        ts = p.get("postedTs") or p.get("t_create") or p.get("creationTs")
        posted, precision = parse_date(ts, self.ctx.now)
        if precision == "datetime" and ts and int(ts) % 86400 == 0:
            precision = "date"  # midnight timestamps are really dates
        locs = p.get("locations") or ([p["location"]] if p.get("location") else [])
        url = p.get("positionUrl") or f"/careers/job/{p.get('id')}"
        job = self.job(p.get("id"), p.get("name") or p.get("posting_name") or "",
                       self.base + url if url.startswith("/") else url,
                       locations=locs, posted_at=posted, posted_precision=precision)
        job.extra["department"] = p.get("department")
        return job

    async def _pcsx(self, location: str | None, query: str | None, max_pages: int) -> list[Job]:
        jobs: list[Job] = []
        for page in range(max_pages):
            params = {"domain": self.params["domain"], "start": page * 10, "sort_by": "timestamp"}
            if location:
                params["location"] = location
            if query:
                params["query"] = query
            data = await self.http.get_json(f"{self.base}/api/pcsx/search", params=params)
            positions = (data.get("data") or {}).get("positions") or []
            jobs.extend(self._to_job(p) for p in positions)
            count = int((data.get("data") or {}).get("count") or 0)
            if not positions or (page + 1) * 10 >= count:
                break
            if positions and all(
                j.posted_at and (self.ctx.now - j.posted_at).days > self.ctx.max_age_days + 1
                for j in jobs[-len(positions):]
            ):
                break
        return jobs

    async def _legacy(self, location: str | None, query: str | None, max_pages: int) -> list[Job]:
        jobs: list[Job] = []
        for page in range(max_pages):
            params = {"domain": self.params["domain"], "start": page * 10, "num": 10, "sort_by": "relevance"}
            if location:
                params["location"] = location
            if query:
                params["query"] = query
            data = await self.http.get_json(f"{self.base}/api/apply/v2/jobs", params=params)
            positions = data.get("positions") or []
            jobs.extend(self._to_job(p) for p in positions)
            if not positions or (page + 1) * 10 >= int(data.get("count") or 0):
                break
        return jobs

    async def fetch(self) -> list[Job]:
        search = self._pcsx if self.params.get("api", "pcsx") == "pcsx" else self._legacy
        try:
            return await self._fetch_with(search)
        except HttpError as exc:
            if search == self._pcsx and exc.status in (401, 403, 404):
                self.params["api"] = "legacy"
                return await self._fetch_with(self._legacy)
            raise

    async def _fetch_with(self, search) -> list[Job]:
        jobs: list[Job] = []
        places = self.ctx.locations.cities() + [country_name(c) for c in self.ctx.locations.countries()]
        if not places and self.full_listing:
            jobs += await search(None, None, self.source.max_pages)
        for place in places:
            if self.full_listing:
                jobs += await search(place, None, self.source.max_pages)
            for q in self.search_terms():
                jobs += await search(place, q, 2)
        for q in self.coverage_terms():
            jobs += await search(None, q, 2)
        return list({j.key: j for j in jobs}.values())

    async def enrich(self, job: Job) -> None:
        if self.params.get("api") == "legacy":
            data = await self.http.get_json(f"{self.base}/api/apply/v2/jobs/{job.native_id}",
                                            params={"domain": self.params["domain"]})
            job.description = html_to_text(data.get("job_description"))
            return
        data = await self.http.get_json(f"{self.base}/api/pcsx/position_details",
                                        params={"position_id": job.native_id, "domain": self.params["domain"]})
        job.description = html_to_text((data.get("data") or {}).get("jobDescription"))
