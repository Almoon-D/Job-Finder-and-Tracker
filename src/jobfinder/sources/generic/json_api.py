"""Declarative adapter for any JSON (or GraphQL) job search API.

Everything site-specific lives in the private config, e.g.::

    - name: Example Bank
      type: json_api
      url: https://careers.example.com/api/search
      method: POST
      body: {"query": "{query}", "country": "{country}", "from": "{offset}", "size": "{limit}"}
      items: "data.jobs"                     # JMESPath to the list of jobs
      fields:
        id: "jobId"
        title: "title"
        url_template: "https://careers.example.com/job/{id}"
        location: "locations[].name"
        posted_at: "postedDate"
        description: "summary"
      pagination: {type: offset, limit: 50}
      detail:                                  # optional, to fetch full descriptions
        url_template: "https://careers.example.com/api/job/{id}"
        description: "job.description"

Placeholders available in url/body/params/headers: {page}, {offset}, {limit},
{query}, {query_slug} ('banca-privada'), {location}, {country} (ISO2), {country_name}.
"""

from __future__ import annotations

import json
import re
from datetime import UTC
from typing import Any

import jmespath

from ...dates import parse_date
from ...matching.location import country_name
from ...models import Job
from ..base import Adapter, AdapterError, register
from ..util import as_list, html_to_text, slug

_PH = re.compile(r"\{(\w+)\}")


def render(value: Any, variables: dict[str, Any]) -> Any:
    """Recursively substitute {placeholders}. A string that is only a placeholder keeps the value's type."""
    if isinstance(value, str):
        m = re.fullmatch(r"\{(\w+)\}", value)
        if m and m.group(1) in variables:
            return variables[m.group(1)]
        return _PH.sub(lambda mm: str(variables.get(mm.group(1), mm.group(0))), value)
    if isinstance(value, dict):
        return {k: render(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, variables) for v in value]
    return value


def extract(expr: str | None, data: Any) -> Any:
    if not expr:
        return None
    return jmespath.search(expr, data)


@register
class JsonApi(Adapter):
    type_name = "json_api"
    supports_search = True

    def _variants(self) -> list[dict[str, Any]]:
        """Combinations of query/location to request."""
        spec = json.dumps({k: self.params.get(k) for k in ("url", "body", "params", "headers")})
        spec = spec.replace("{query_slug}", "{query}")
        queries = self.search_terms() if "{query}" in spec else []
        base: list[dict[str, Any]] = [{"query": q} for q in queries] or [{"query": ""}]
        uses_loc = any(p in spec for p in ("{location}", "{country}", "{country_name}"))
        variants: list[dict[str, Any]] = []
        if uses_loc:
            places: list[dict[str, Any]] = []
            if "{location}" in spec:
                places += [{"location": c} for c in self.params.get("location_values") or self.ctx.locations.cities()]
            if "{country}" in spec or "{country_name}" in spec:
                places += [{"country": c, "country_name": country_name(c)} for c in self.ctx.locations.countries()]
            for b in base:
                for pl in places or [{}]:
                    variants.append({**b, **pl})
        else:
            variants = base
        if "{query}" in spec:
            variants += [{"query": q, "_coverage": True} for q in self.coverage_terms()]
        return variants

    async def _request(self, variables: dict[str, Any]) -> Any:
        p = self.params
        url = render(p["url"], variables)
        method = (p.get("method") or "GET").upper()
        headers = render(p.get("headers") or {}, variables)
        params = render(p.get("params"), variables) if p.get("params") else None
        if method == "POST":
            return await self.http.post_json(url, json=render(p.get("body") or {}, variables), headers=headers,
                                             params=params)
        return await self.http.get_json(url, headers=headers, params=params)

    def _to_job(self, item: Any) -> Job | None:
        f = self.params.get("fields") or {}
        values: dict[str, Any] = {}
        for name in ("id", "title", "url", "location", "posted_at", "description", "company"):
            values[name] = extract(f.get(name), item)
        if not values["title"]:
            return None
        if not values["url"] and f.get("url_template"):
            fmt_vars = {k: v for k, v in values.items() if v is not None}
            if isinstance(item, dict):
                fmt_vars = {**{k: v for k, v in item.items() if isinstance(v, str | int)}, **fmt_vars}
            values["url"] = _PH.sub(lambda m: str(fmt_vars.get(m.group(1), "")), f["url_template"])
        native = values["id"] if values["id"] is not None else values["url"]
        posted, precision = parse_date(values["posted_at"], self.ctx.now)
        if f.get("posted_at_format") and isinstance(values["posted_at"], str):
            from datetime import datetime

            try:
                posted = datetime.strptime(values["posted_at"], f["posted_at_format"]).replace(tzinfo=UTC)
                precision = "datetime" if "%H" in f["posted_at_format"] else "date"
            except ValueError:
                pass
        locs = [str(x).strip(" ,") for x in as_list(values["location"]) if str(x).strip(" ,")]
        job = self.job(native, str(values["title"]), str(values["url"] or ""), locations=locs,
                       posted_at=posted, posted_precision=precision,
                       description=html_to_text(values["description"]) if values["description"] else "",
                       company=values["company"])
        job.extra["item"] = item if len(json.dumps(item, default=str)) < 4000 else None
        return job

    async def fetch(self) -> list[Job]:
        if "url" not in self.params or "items" not in self.params:
            raise AdapterError("json_api needs 'url' and 'items'")
        pag = self.params.get("pagination") or {}
        ptype = pag.get("type", "none")
        limit = int(pag.get("limit", 20))
        start = int(pag.get("start", 0 if ptype == "offset" else 1))
        max_pages = int(pag.get("max_pages", self.source.max_pages))
        jobs: dict[str, Job] = {}
        for var in self._variants():
            pages = 2 if var.get("_coverage") else (max_pages if ptype != "none" else 1)
            for page in range(pages):
                variables = {
                    **var,
                    "query_slug": slug(var.get("query", "")),
                    "limit": limit,
                    "page": start + page,
                    "offset": start + page * limit,
                }
                data = await self._request(variables)
                items = as_list(extract(self.params["items"], data))
                for it in items:
                    job = self._to_job(it)
                    if job:
                        jobs.setdefault(job.key, job)
                if len(items) < limit:
                    break
        return list(jobs.values())

    async def enrich(self, job: Job) -> None:
        detail = self.params.get("detail")
        if not detail:
            return
        item = job.extra.get("item") or {}
        variables = {"id": job.native_id, "url": job.url, **(item if isinstance(item, dict) else {})}
        url = _PH.sub(lambda m: str(variables.get(m.group(1), "")), detail["url_template"])
        if detail.get("format") == "html":
            from selectolax.parser import HTMLParser

            html = await self.http.get_text(url)
            node = HTMLParser(html).css_first(detail.get("description") or "body")
            job.description = html_to_text(node.html if node else "")
            return
        data = await self.http.get_json(url, headers=self.params.get("headers") or {})
        job.description = html_to_text(extract(detail.get("description"), data) or "")
        loc = extract(detail.get("location"), data)
        if loc:
            job.locations = [str(x) for x in as_list(loc) if x]
