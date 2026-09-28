"""Adzuna job search aggregator through its official API. Needs free developer keys.

    - name: Adzuna
      type: adzuna
      group: boards
      queries: [private banker, banca privada]
      countries: [ES, CH, MX]      # default: the countries of your configured locations
      where: [Madrid]              # optional per-country override: {ES: [Madrid], CH: [Geneva, Lausanne]}
      max_pages: 1                 # 50 jobs per page

Create an application at https://developer.adzuna.com and store its id and key as the
ADZUNA_APP_ID and ADZUNA_APP_KEY secrets (names configurable with ``app_id_env`` /
``app_key_env``). Without them the source is skipped. Free keys have a daily request cap,
so keep queries × places small.
"""

from __future__ import annotations

import asyncio
import math
import os
from typing import Any

from ...dates import parse_date
from ...matching.location import resolve_country
from ...models import Job
from ..base import Adapter, register
from ..util import html_to_text, skip_without

API = "https://api.adzuna.com/v1/api/jobs/{cc}/search/{page}"
PAGE_SIZE = 50
PAUSE = 2.5  # free keys allow about 25 requests per minute
SUPPORTED = {"AT", "AU", "BE", "BR", "CA", "CH", "DE", "ES", "FR", "GB", "IN", "IT", "MX", "NL", "NZ", "PL",
             "SG", "US", "ZA"}


@register
class Adzuna(Adapter):
    type_name = "adzuna"
    supports_search = True

    def _credentials(self) -> dict[str, str]:
        id_env = self.params.get("app_id_env", "ADZUNA_APP_ID")
        key_env = self.params.get("app_key_env", "ADZUNA_APP_KEY")
        skip_without(id_env, key_env)
        return {"app_id": os.environ[id_env], "app_key": os.environ[key_env]}

    def _countries(self) -> list[str]:
        wanted = self.params.get("countries") or self.ctx.locations.countries()
        return [c for c in (resolve_country(str(x)) for x in wanted) if c in SUPPORTED]

    def _places(self, country: str) -> list[str | None]:
        where = self.params.get("where")
        if isinstance(where, dict):
            return list(where.get(country) or [None])
        if where:
            return list(where)
        cities = [t.label for t in self.ctx.locations.targets if t.label and t.country == country]
        return list(cities) or [None]

    def _to_job(self, r: dict[str, Any]) -> Job | None:
        if not r.get("id") or not r.get("title") or not r.get("redirect_url"):
            return None
        posted, precision = parse_date(r.get("created"), self.ctx.now)
        loc = r.get("location") or {}
        area = [str(a) for a in loc.get("area") or [] if a]  # country first: ["España", "Madrid", "Madrid"]
        place = ", ".join(dict.fromkeys(reversed(area))) or loc.get("display_name") or ""
        return self.job(r["id"], html_to_text(r["title"]), r["redirect_url"],
                        company=(r.get("company") or {}).get("display_name") or None,
                        locations=[place] if place else [],
                        posted_at=posted, posted_precision=precision, description=html_to_text(r.get("description")))

    async def fetch(self) -> list[Job]:
        auth = self._credentials()
        days = max(1, math.ceil(self.ctx.max_age_days))
        jobs: list[Job] = []
        for cc in self._countries():
            for q in self.search_terms():
                for where in self._places(cc):
                    for page in range(1, self.source.max_pages + 1):
                        params: dict[str, Any] = {**auth, "what": q, "max_days_old": days, "sort_by": "date",
                                                  "results_per_page": PAGE_SIZE}
                        if where:
                            params["where"] = where
                        data = await self.http.get_json(API.format(cc=cc.lower(), page=page), params=params)
                        await asyncio.sleep(PAUSE)
                        results = data.get("results") or []
                        jobs += [j for j in map(self._to_job, results) if j]
                        if len(results) < PAGE_SIZE:
                            break
        return list({j.key: j for j in jobs}.values())
