"""InfoJobs (Spain) through its official API. Needs free developer credentials.

    - name: InfoJobs
      type: infojobs
      group: boards
      queries: [banca privada, relación con inversores]
      provinces: [madrid, barcelona]   # optional: InfoJobs province keys; default all of Spain
      max_pages: 2                     # 50 offers per page

Register an application at https://developer.infojobs.net and store its client id and
secret as the INFOJOBS_CLIENT_ID and INFOJOBS_CLIENT_SECRET secrets (names configurable
with ``client_id_env`` / ``client_secret_env``). Without them the source is skipped.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from ...dates import parse_date
from ...models import Job
from ..base import Adapter, register
from ..util import skip_without

API = "https://api.infojobs.net/api/9/offer"
PAGE_SIZE = 50


def since_date(max_age_days: float) -> str:
    if max_age_days <= 1:
        return "_24_HOURS"
    return "_7_DAYS" if max_age_days <= 7 else "_15_DAYS"


@register
class InfoJobs(Adapter):
    type_name = "infojobs"
    supports_search = True

    def _auth(self) -> dict[str, str]:
        id_env = self.params.get("client_id_env", "INFOJOBS_CLIENT_ID")
        secret_env = self.params.get("client_secret_env", "INFOJOBS_CLIENT_SECRET")
        skip_without(id_env, secret_env)
        token = base64.b64encode(f"{os.environ[id_env]}:{os.environ[secret_env]}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _to_job(self, o: dict[str, Any]) -> Job | None:
        if not o.get("id") or not o.get("title") or not o.get("link"):
            return None
        place = ", ".join(x for x in (o.get("city"), (o.get("province") or {}).get("value"), "España") if x)
        posted, precision = parse_date(o.get("published") or o.get("updated"), self.ctx.now)
        return self.job(o["id"], o["title"], o["link"], company=(o.get("author") or {}).get("name") or None,
                        locations=[place], posted_at=posted, posted_precision=precision,
                        description=" · ".join(x for x in (o.get("requirementMin"),
                                                         (o.get("experienceMin") or {}).get("value")) if x))

    async def fetch(self) -> list[Job]:
        headers = self._auth()
        jobs: list[Job] = []
        for q in self.search_terms() + self.coverage_terms():
            for province in self.params.get("provinces") or [None]:
                for page in range(1, self.source.max_pages + 1):
                    params: dict[str, Any] = {"q": q, "sinceDate": since_date(self.ctx.max_age_days),
                                              "order": "updated-desc", "maxResults": PAGE_SIZE, "page": page}
                    if province:
                        params["province"] = province
                    data = await self.http.get_json(API, params=params, headers=headers)
                    offers = data.get("offers") or []
                    jobs += [j for j in map(self._to_job, offers) if j]
                    if len(offers) < PAGE_SIZE or page >= int(data.get("totalPages") or 0):
                        break
        return list({j.key: j for j in jobs}.values())
