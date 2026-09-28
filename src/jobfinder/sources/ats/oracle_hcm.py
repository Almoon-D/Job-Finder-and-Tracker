"""Oracle Recruiting Cloud (Oracle HCM "Candidate Experience") public REST API."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit

from ...dates import parse_date
from ...matching.location import country_name
from ...models import Job
from ..base import Adapter, register
from ..util import html_to_text

_SITE = re.compile(r"/hcmUI/CandidateExperience/(?P<lang>[\w-]+)/sites/(?P<site>[\w-]+)", re.I)
_HOST = re.compile(r"\.oraclecloud\.[a-z]{2,3}$", re.I)  # .com, .eu, ... (regional data centres: fa.ocs.oraclecloud.eu)
PAGE = 25


@register
class OracleHCM(Adapter):
    type_name = "oracle_hcm"
    supports_search = True

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        parts = urlsplit(url)
        m = _SITE.search(parts.path)
        if m and _HOST.search(parts.netloc):
            return {"host": parts.netloc, "site": m["site"], "lang": m["lang"]}
        return None

    def _url(self, finder: str, expand: str = "requisitionList.secondaryLocations") -> str:
        host = self.params["host"]
        return (
            f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand={expand}&finder=findReqs;{finder}"
        )

    def public_url(self, req_id: str) -> str:
        p = self.params
        return f"https://{p['host']}/hcmUI/CandidateExperience/{p.get('lang', 'en')}/sites/{p['site']}/job/{req_id}"

    async def _search(self, location: str | None, keyword: str | None, max_pages: int) -> list[Job]:
        jobs: list[Job] = []
        cutoff_days = self.ctx.max_age_days + 1
        for page in range(max_pages):
            parts = [f"siteNumber={self.params['site']}", f"limit={PAGE}", f"offset={page * PAGE}",
                     "sortBy=POSTING_DATES_DESC"]
            if location:
                parts.append(f"location={quote(location)}")
            if keyword:
                parts.append(f"keyword={quote(keyword)}")
            data = await self.http.get_json(self._url(",".join(parts)))
            item = (data.get("items") or [{}])[0]
            reqs = item.get("requisitionList") or []
            too_old = False
            for r in reqs:
                job = self._to_job(r)
                jobs.append(job)
                if job.posted_at and (self.ctx.now - job.posted_at).days > cutoff_days:
                    too_old = True
            total = int(item.get("TotalJobsCount") or 0)
            if not reqs or too_old or (page + 1) * PAGE >= total:
                break
        return jobs

    def _to_job(self, r: dict[str, Any]) -> Job:
        posted, precision = parse_date(r.get("PostedDate"), self.ctx.now)
        locs = [r.get("PrimaryLocation")] + [s.get("Name") for s in r.get("secondaryLocations") or []]
        return self.job(
            r.get("Id"), r.get("Title", ""), self.public_url(str(r.get("Id"))),
            locations=[x for x in locs if x], posted_at=posted, posted_precision=precision,
            description=html_to_text(r.get("ShortDescriptionStr")),
        )

    async def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        countries = self.ctx.locations.countries()
        if not countries and self.full_listing:
            jobs += await self._search(None, None, self.source.max_pages)
        for iso2 in countries:
            if self.full_listing:
                jobs += await self._search(country_name(iso2), None, self.source.max_pages)
            for q in self.search_terms():
                jobs += await self._search(country_name(iso2), q, 2)
        for q in self.coverage_terms():
            jobs += await self._search(None, q, 2)
        return list({j.key: j for j in jobs}.values())

    async def enrich(self, job: Job) -> None:
        host, site = self.params["host"], self.params["site"]
        url = (
            f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            f"?expand=all&onlyData=true&finder=ById;Id=%22{job.native_id}%22,siteNumber={site}"
        )
        data = await self.http.get_json(url)
        item = (data.get("items") or [{}])[0]
        parts = [item.get("ExternalDescriptionStr"), item.get("ExternalResponsibilitiesStr"),
                 item.get("ExternalQualificationsStr")]
        job.description = html_to_text(" ".join(p for p in parts if p))
