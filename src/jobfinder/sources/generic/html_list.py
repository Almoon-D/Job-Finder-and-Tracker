"""Declarative adapter for HTML job lists (CSS selectors in the private config).

    - name: Example Recruiter
      type: html_list
      url: https://recruiter.example/jobs?page={page}
      item: "article.job"          # one node per job
      title: "h2"                  # text of this node
      link: "a"                    # href of this node (default: first <a>)
      # cards without a link: id_attr: data-id + url_template: "https://recruiter.example/job/{id}"
      company_selector: ".company" # optional (default: the source name)
      location: ".job-location"
      date: ".job-date"
      date_regex: "(\\d{2}/\\d{2}/\\d{4})"  # optional: the date part of the date text
      location_regex: "·\\s*(.+)$"      # optional: the place part of the location text
      pagination: {start: 1, max_pages: 3}
      delay_seconds: 0             # optional pause between listing requests (honour robots.txt Crawl-delay)
      fetch: http                  # or "browser" for JavaScript-rendered pages (Playwright)
      detail: {description: ".job-description", jsonld: true}   # jsonld: JobPosting on the job page

``urls`` (a list) replaces ``url`` to read several listing pages in one source. A 404 on a
later page or on a keyword search means "no more results" (many sites answer that way).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from selectolax.parser import HTMLParser, Node

from ...dates import parse_date
from ...models import Job
from ..base import Adapter, AdapterError, register
from ..http import HttpError
from ..util import absolute, as_list, html_to_text, slug
from .json_api import render
from .jsonld import job_postings, posting_locations


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


def text_part(text: str, pattern: str | None) -> str:
    """The part of ``text`` that ``pattern`` matches (its groups joined), e.g. the date in 'Online since: <date>'."""
    if not pattern or not text:
        return text
    m = re.search(pattern, text)
    if not m:
        return ""
    return " ".join(g for g in m.groups() if g) if m.groups() else m.group(0)


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
            native = (node.attributes.get(p["id_attr"]) or "").strip() if p.get("id_attr") else ""
            if p.get("url_template") and native:
                href = p["url_template"].replace("{id}", native)
            else:
                link_node = node.css_first(p.get("link") or "a") if p.get("link") != "self" else node
                href = link_node.attributes.get("href") if link_node else None
            if not title or not href:
                continue
            url = absolute(page_url, href)
            posted, precision = parse_date(text_part(_text(node, p.get("date")), p.get("date_regex")), self.ctx.now)
            loc = _text(node, p.get("location"))
            loc = text_part(loc, p.get("location_regex")) or loc  # no match: keep the whole text
            jobs.append(self.job(native or url, title, url, locations=[loc] if loc else [],
                                 posted_at=posted, posted_precision=precision,
                                 description=_text(node, p.get("summary")),
                                 company=_text(node, p.get("company_selector")) or None))
        return jobs

    async def fetch(self) -> list[Job]:
        p = self.params
        templates = [str(u) for u in as_list(p.get("urls") or p.get("url"))]
        if not templates or not p.get("item"):
            raise AdapterError("html_list needs 'url' (or 'urls') and 'item'")
        pag = p.get("pagination") or {}
        start = int(pag.get("start", 1))
        delay = float(p.get("delay_seconds") or 0)
        sent = 0
        out: dict[str, Job] = {}
        for template in templates:
            max_pages = int(pag.get("max_pages", 1 if "{page}" not in template else 3))
            searching = bool(re.search(r"\{query(_slug)?\}", template))
            for q in self.search_terms() if searching else [""]:
                for page in range(max_pages):
                    url = render(template, {"page": start + page, "offset": page * int(pag.get("limit", 10)),
                                            "query": q, "query_slug": slug(q)})
                    if delay and sent:
                        await asyncio.sleep(delay)
                    sent += 1
                    try:
                        html = await fetch_page(self, url, p.get("fetch"))
                    except HttpError as exc:
                        # Past the last page, or a search without results: many sites answer 404.
                        if exc.status == 404 and (page > 0 or searching):
                            break
                        raise
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
        described = bool(detail.get("jsonld")) and self._from_jsonld(job, html)
        tree = HTMLParser(html)
        if detail.get("description") or not described:
            node = tree.css_first(detail.get("description") or "body")
            job.description = html_to_text(node.html if node else "")
        if detail.get("location"):
            loc = _text(tree.root, detail["location"]) if tree.root else ""
            if loc:
                job.locations = [loc]
        if detail.get("date") and job.posted_at is None:
            text = text_part(_text(tree.root, detail["date"]), self.params.get("date_regex"))
            job.posted_at, job.posted_precision = parse_date(text, self.ctx.now)

    def _from_jsonld(self, job: Job, html: str) -> bool:
        """Description, date and (missing) locations from the JobPosting JSON-LD of the job page.

        Returns whether the posting had a description."""
        postings = job_postings(html)
        if not postings:
            return False
        posting = next((pp for pp in postings
                        if isinstance(pp.get("url"), str) and absolute(job.url, pp["url"]) == job.url), postings[0])
        job.description = html_to_text(posting.get("description")) or job.description
        if job.posted_at is None:
            job.posted_at, job.posted_precision = parse_date(posting.get("datePosted"), self.ctx.now)
        if not job.locations:
            job.locations = posting_locations(posting)
        return bool(posting.get("description"))
