"""Declarative adapter for HTML job lists (CSS selectors in the private config).

    - name: Example Recruiter
      type: html_list
      url: https://recruiter.example/jobs?page={page}
      item: "article.job"          # one node per job
      title: "h2"                  # text of this node
      link: "a"                    # href of this node (default: first <a>)
      location: ".job-location"
      date: ".job-date"
      pagination: {start: 1, max_pages: 3}
      fetch: http                  # or "browser" for JavaScript-rendered pages (Playwright)
      detail: {description: ".job-description"}
"""

from __future__ import annotations

from typing import Any

from selectolax.parser import HTMLParser, Node

from ...dates import parse_date
from ...models import Job
from ..base import Adapter, AdapterError, register
from ..util import absolute, html_to_text
from .json_api import render


async def fetch_page(adapter: Adapter, url: str, mode: str | None) -> str:
    if mode == "browser":
        from .browser import render_page

        return await render_page(url, wait_for=adapter.params.get("wait_for"))
    return await adapter.http.get_text(url, headers=adapter.params.get("headers") or {})


def _text(node: Node, selector: str | None) -> str:
    if not selector:
        return ""
    n = node.css_first(selector)
    return n.text(separator=" ", strip=True) if n else ""


@register
class HtmlList(Adapter):
    type_name = "html_list"
    supports_search = True

    @property
    def needs_browser_fetch(self) -> bool:
        return self.params.get("fetch") == "browser"

    def _parse(self, html: str, page_url: str) -> list[Job]:
        p = self.params
        tree = HTMLParser(html)
        jobs = []
        for node in tree.css(p["item"]):
            title = _text(node, p.get("title")) or node.text(separator=" ", strip=True)[:200]
            link_node = node.css_first(p.get("link") or "a") if p.get("link") != "self" else node
            href = link_node.attributes.get("href") if link_node else None
            if not title or not href:
                continue
            url = absolute(page_url, href)
            posted, precision = parse_date(_text(node, p.get("date")), self.ctx.now)
            loc = _text(node, p.get("location"))
            native = node.attributes.get(p["id_attr"]) if p.get("id_attr") else None
            jobs.append(self.job(native or url, title, url, locations=[loc] if loc else [],
                                 posted_at=posted, posted_precision=precision,
                                 description=_text(node, p.get("summary"))))
        return jobs

    async def fetch(self) -> list[Job]:
        p = self.params
        if not p.get("url") or not p.get("item"):
            raise AdapterError("html_list needs 'url' and 'item'")
        pag = p.get("pagination") or {}
        start = int(pag.get("start", 1))
        max_pages = int(pag.get("max_pages", 1 if "{page}" not in p["url"] else 3))
        queries = self.search_terms() if "{query}" in p["url"] else [""]
        out: dict[str, Job] = {}
        for q in queries:
            for page in range(max_pages):
                url = render(p["url"], {"page": start + page, "offset": page * int(pag.get("limit", 10)),
                                        "query": q})
                html = await fetch_page(self, url, p.get("fetch"))
                found = self._parse(html, url)
                new = [j for j in found if j.key not in out]
                for j in new:
                    out[j.key] = j
                if not new:
                    break
        return list(out.values())

    async def enrich(self, job: Job) -> None:
        detail: dict[str, Any] = self.params.get("detail") or {}
        if not detail:
            return
        html = await fetch_page(self, job.url, self.params.get("fetch"))
        tree = HTMLParser(html)
        node = tree.css_first(detail.get("description") or "body")
        job.description = html_to_text(node.html if node else "")
        if detail.get("location"):
            loc = _text(tree.root, detail["location"]) if tree.root else ""
            if loc:
                job.locations = [loc]
        if detail.get("date") and job.posted_at is None:
            job.posted_at, job.posted_precision = parse_date(_text(tree.root, detail["date"]), self.ctx.now)
