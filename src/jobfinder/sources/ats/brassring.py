"""IBM/Infinite BrassRing "Talent Gateway" career sites (…/TGnewUI/Search/Home/Home?partnerid=…&siteid=…).

The public search page embeds a session token; with it the same JSON endpoints the
page uses return every open job, newest first (50 per page).
"""

from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ...dates import parse_date
from ...models import Job
from ..base import Adapter, AdapterError, register
from ..util import html_to_text

_RESULTS = re.compile(r'id="searchResults"[^>]*?\svalue="([^"]*)"')
_TOKEN = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"|'
                    r'__RequestVerificationToken" type="hidden" value="([^"]+)"')


@register
class BrassRing(Adapter):
    type_name = "brassring"

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        parts = urlsplit(url)
        if "/tgnewui/" not in parts.path.lower():
            return None
        qs = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}
        if "partnerid" in qs and "siteid" in qs:
            return {"host": parts.netloc, "partnerid": qs["partnerid"], "siteid": qs["siteid"]}
        return None

    @property
    def base(self) -> str:
        return f"https://{self.params['host']}"

    async def _session(self) -> tuple[dict[str, Any], str]:
        p = self.params
        page = await self.http.get_text(
            f"{self.base}/TGnewUI/Search/Home/Home", params={"partnerid": p["partnerid"], "siteid": p["siteid"]}
        )
        m, t = _RESULTS.search(page), _TOKEN.search(page)
        if not m or not t:
            raise AdapterError("brassring: search page without session data")
        return json.loads(html.unescape(m.group(1))), t.group(1) or t.group(2)

    def _to_job(self, raw: dict[str, Any], location_fields: list[str]) -> Job:
        q = {x.get("QuestionName", "").lower(): x.get("Value") for x in raw.get("Questions") or []}
        posted, _ = parse_date(q.get("lastupdated"), self.ctx.now)
        locs = [str(q[f]).strip() for f in location_fields if q.get(f)]
        job = self.job(q.get("reqid"), html_to_text(q.get("jobtitle") or ""), raw.get("Link") or "",
                       locations=[", ".join(dict.fromkeys(locs))] if locs else [],
                       posted_at=posted, posted_precision="date" if posted else "unknown",
                       description=html_to_text(q.get("jobdescription")))
        return job

    async def fetch(self) -> list[Job]:
        data, token = await self._session()
        loc_fields = [f.strip().lower() for f in str(data.get("LocationCustomSolrFields") or "").split(",")
                      if f.strip() and f.strip().lower() != "location"]
        body = {
            "partnerId": self.params["partnerid"], "siteId": self.params["siteid"], "keyword": "", "location": "",
            "keywordCustomSolrFields": data.get("KeywordCustomSolrFields"),
            "locationCustomSolrFields": data.get("LocationCustomSolrFields"),
            "linkId": "", "Latitude": 0, "Longitude": 0, "facetfilterfields": {"Facet": []},
            "powersearchoptions": {"PowerSearchOption": []}, "SortType": "LastUpdated",
            "encryptedSessionValue": data.get("EncryptedSession"),
        }
        headers = {"RFT": token, "X-Requested-With": "XMLHttpRequest"}
        jobs: list[Job] = []
        for page in range(1, self.source.max_pages + 1):
            path = "PowerSearchJobs" if page == 1 else "ProcessSortAndShowMoreJobs"
            resp = await self.http.post_json(f"{self.base}/TgNewUI/Search/Ajax/{path}",
                                             json={**body, "pageNumber": page}, headers=headers)
            batch = [self._to_job(r, loc_fields) for r in (resp.get("Jobs") or {}).get("Job") or []]
            jobs.extend(batch)
            total = int(resp.get("JobsCount") or 0)
            oldest = min((j.posted_at for j in batch if j.posted_at), default=None)
            if (not batch or len(jobs) >= total
                    or (oldest and (self.ctx.now - oldest).days > self.ctx.max_age_days + 1)):
                break
        return list({j.key: j for j in jobs}.values())
