"""RSS 2.0 / Atom job feeds.

    - name: Example feed
      type: rss
      url: https://example.com/jobs/feed
      location_regex: "Location:\\s*([^<\\n]+)"   # optional, applied to the description
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser, Node

from ...dates import parse_date
from ...models import Job
from ..base import Adapter, AdapterError, register
from ..util import html_to_text


def _txt(node: Node, *tags: str) -> str:
    for tag in tags:
        n = node.css_first(tag)
        if n is not None:
            return (n.text(strip=True) or n.attributes.get("href") or "").strip()
    return ""


@register
class Rss(Adapter):
    type_name = "rss"

    async def fetch(self) -> list[Job]:
        url = self.params.get("url")
        if not url:
            raise AdapterError("rss needs 'url'")
        xml = await self.http.get_text(url)
        # selectolax is an HTML parser: <link> is a void element there, so rename it first.
        xml = re.sub(r"<(/?)link\b", r"<\1jflink", xml)
        tree = HTMLParser(xml)
        loc_rx = re.compile(self.params["location_regex"]) if self.params.get("location_regex") else None
        jobs = []
        for item in tree.css("item") or tree.css("entry"):
            title = html_to_text(_txt(item, "title"))
            link = _txt(item, "jflink", "guid", "id")
            if not title or not link:
                continue
            desc_raw = _txt(item, "description", "summary", "content")
            desc = html_to_text(desc_raw)
            posted, precision = parse_date(_txt(item, "pubdate", "published", "updated", "dc\\:date"), self.ctx.now)
            locs = []
            if loc_rx:
                m = loc_rx.search(desc_raw) or loc_rx.search(desc)
                if m:
                    locs.append(m.group(1).strip())
            locs += [c.text(strip=True) for c in item.css(self.params.get("location_tag", "_none_"))]
            guid = _txt(item, "guid", "id") or link
            jobs.append(self.job(guid, title, link, locations=locs, posted_at=posted, posted_precision=precision,
                                 description=desc))
        return jobs
