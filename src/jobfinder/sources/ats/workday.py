"""Workday (myworkdayjobs.com / myworkdaysite.com) public job search API."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from ...dates import parse_date
from ...models import Job
from ..base import Adapter, register
from ..util import html_to_text

_WD_HOST = re.compile(r"^(?P<tenant>[\w-]+)\.(?P<pod>wd\d+)\.myworkdayjobs\.com$", re.I)
_LANG = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
PAGE = 20


@register
class Workday(Adapter):
    type_name = "workday"
    supports_search = True

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        parts = urlsplit(url)
        segs = [s for s in parts.path.split("/") if s]
        m = _WD_HOST.match(parts.netloc)
        if m:
            segs = [s for s in segs if not _LANG.match(s)]
            if not segs:
                return None
            return {"host": parts.netloc, "tenant": m["tenant"], "site": segs[0]}
        if parts.netloc.endswith("myworkdaysite.com") and len(segs) >= 3 and segs[0] == "recruiting":
            return {"host": parts.netloc, "tenant": segs[1], "site": segs[2]}
        return None

    @property
    def api(self) -> str:
        p = self.params
        return f"https://{p['host']}/wday/cxs/{p['tenant']}/{p['site']}"

    def public_url(self, external_path: str) -> str:
        p = self.params
        if p["host"].endswith("myworkdaysite.com"):
            return f"https://{p['host']}/recruiting/{p['tenant']}/{p['site']}{external_path}"
        return f"https://{p['host']}/{p['site']}{external_path}"

    async def _page(self, offset: int, search: str, facets: dict[str, list[str]]) -> dict[str, Any]:
        body = {"appliedFacets": facets, "limit": PAGE, "offset": offset, "searchText": search}
        return await self.http.post_json(f"{self.api}/jobs", json=body)

    @staticmethod
    def _walk_facets(facets: list[dict[str, Any]], parent: str | None = None):
        for f in facets or []:
            param = f.get("facetParameter") or parent
            values = f.get("values") or []
            for v in values:
                if "values" in v:  # nested group
                    yield from Workday._walk_facets([v], param)
                elif v.get("id"):
                    yield param, v.get("id"), v.get("descriptor") or "", int(v.get("count") or 0)

    def location_facets(self, facets: list[dict[str, Any]]) -> dict[str, list[str]] | None:
        """Pick the location facet values that match the configured locations.

        Returns None when no location facet exists (then we filter client-side only),
        or {} when the site has no job in any configured location.
        """
        matcher = self.ctx.locations
        by_param: dict[str, list[tuple[str, int]]] = {}
        seen_location_param = False
        for param, vid, desc, count in self._walk_facets(facets):
            if not param or not re.search(r"location|country|region", param, re.I):
                continue
            seen_location_param = True
            if matcher.match_location(desc) or matcher.is_country_of_target(desc):
                by_param.setdefault(param, []).append((vid, count))
        if not seen_location_param:
            return None
        if not by_param:
            return {}
        # Facets of different parameters are ANDed by Workday: use the broadest one only.
        param, vals = max(by_param.items(), key=lambda kv: sum(c for _, c in kv[1]))
        return {param: [v for v, _ in vals]}

    def _to_job(self, p: dict[str, Any]) -> Job:
        path = p.get("externalPath") or ""
        bullets = [str(b) for b in p.get("bulletFields") or []]
        date_text = p.get("postedOn") or next((b for b in bullets if re.search(r"post|publi|date", b, re.I)), None)
        if date_text and ":" in date_text and not p.get("postedOn"):
            date_text = date_text.split(":", 1)[1]
        posted, precision = parse_date(date_text, self.ctx.now)
        loc = p.get("locationsText") or ""
        if not loc:
            # Some tenants put the location in the first bullet or only in the URL path.
            first = bullets[0] if bullets else ""
            slug = path.split("/")[2] if path.count("/") >= 3 else ""
            loc = first if first and not re.search(r"\d{4,}|:", first) else slug.replace("-", " ")
        job = self.job(path, p.get("title", ""), self.public_url(path), locations=[loc] if loc else [],
                       posted_at=posted, posted_precision=precision)
        job.extra["path"] = path
        job.extra["multi_location"] = bool(re.match(r"^\d+ Locations$", loc))
        return job

    async def _collect(self, search: str, facets: dict[str, list[str]], max_pages: int) -> list[Job]:
        jobs: list[Job] = []
        total = None
        for page in range(max_pages):
            data = await self._page(page * PAGE, search, facets)
            postings = data.get("jobPostings") or []
            if total is None:
                total = int(data.get("total") or 0)
            jobs.extend(self._to_job(p) for p in postings)
            if not postings or (page + 1) * PAGE >= (total or 0):
                break
        return jobs

    async def fetch(self) -> list[Job]:
        first = await self._page(0, "", {})
        facets = self.location_facets(first.get("facets") or [])
        jobs: list[Job] = []
        if self.full_listing and facets != {}:  # {} = none of your locations exists on this site
            jobs += await self._collect("", facets or {}, self.source.max_pages)
        for q in self.search_terms():
            jobs += await self._collect(q, facets or {}, 3)
        for q in self.coverage_terms():
            jobs += await self._collect(q, {}, 2)
        return list({j.key: j for j in jobs}.values())

    async def enrich(self, job: Job) -> None:
        data = await self.http.get_json(f"{self.api}{job.extra.get('path', '')}")
        info = data.get("jobPostingInfo") or {}
        job.description = html_to_text(info.get("jobDescription"))
        locs = [info.get("location"), *(info.get("additionalLocations") or [])]
        locs = [x for x in locs if x]
        if locs:
            job.locations = locs
        if info.get("startDate"):
            posted, _ = parse_date(info["startDate"], self.ctx.now)
            # Keep the relative estimate (it has a time of day) unless the dates disagree.
            if (posted and job.posted_precision in ("relative", "unknown")
                    and (job.posted_at is None or abs((job.posted_at - posted).days) > 1)):
                job.posted_at, job.posted_precision = posted, "date"
