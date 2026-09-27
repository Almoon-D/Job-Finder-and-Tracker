"""Watch a careers page that has no ATS or structured list.

Two modes:

* ``link_pattern`` given: every new link whose URL matches the regex becomes a job
  (title = link text). Good for small firms that list openings as links.
* no ``link_pattern``: any change of the (normalised) page content produces a
  "page changed" notification with the page link.

    - name: Small Family Office
      type: page_monitor
      url: https://example-fo.com/careers
      selector: "main"                # optional: only watch this part of the page
      link_pattern: "/(careers|jobs|empleo)/.+"
"""

from __future__ import annotations

import hashlib
import re

from selectolax.parser import HTMLParser

from ...models import Job
from ..base import Adapter, AdapterError, register
from ..util import absolute
from .html_list import fetch_page

MAX_LINKS = 500


@register
class PageMonitor(Adapter):
    type_name = "page_monitor"

    @property
    def needs_browser_fetch(self) -> bool:
        return self.params.get("fetch") == "browser"

    async def fetch(self) -> list[Job]:
        url = self.params.get("url")
        if not url:
            raise AdapterError("page_monitor needs 'url'")
        html = await fetch_page(self, url, self.params.get("fetch"))
        tree = HTMLParser(html)
        for n in tree.css("script, style, noscript, svg"):
            n.decompose()
        root = tree.css_first(self.params["selector"]) if self.params.get("selector") else tree.body
        if root is None:
            raise AdapterError("page_monitor: selector not found")
        memory = self.ctx.state.monitor(self.source.key)
        first_time = "hash" not in memory

        if self.params.get("link_pattern"):
            rx = re.compile(self.params["link_pattern"])
            links: dict[str, str] = {}
            for a in root.css("a[href]"):
                href = absolute(url, a.attributes.get("href"))
                if rx.search(href):
                    links.setdefault(href.split("#")[0], a.text(separator=" ", strip=True))
            known = set(memory.get("links", []))
            new_hash = hashlib.sha1("\n".join(sorted(links)).encode()).hexdigest()
            if memory.get("hash") != new_hash:
                self.ctx.state.material = True
            memory["links"] = sorted(links)[:MAX_LINKS]
            memory["hash"] = new_hash
            if first_time:
                return []
            return [self.job(href, text or href.rsplit("/", 1)[-1], href) for href, text in links.items()
                    if href not in known]

        text = re.sub(r"\s+", " ", root.text(separator=" ")).strip()
        digest = hashlib.sha1(text.encode()).hexdigest()
        previous = memory.get("hash")
        memory["hash"] = digest
        if previous != digest:
            self.ctx.state.material = True
        if first_time or previous == digest:
            return []
        job = self.job(digest[:12], self.params.get("change_title") or "Careers page updated", url)
        job.kind = "page_change"
        return [job]
