"""Turn job-alert e-mails into jobs.

Known senders (LinkedIn, Indeed, InfoJobs, eFinancialCareers, jobup/jobs.ch, Michael Page)
are parsed without AI: every link is unwrapped from its click-tracking redirect, links
that point to a job of that site are reduced to the job's canonical URL, the link text is
the title and the following lines of the same block are the company and the location.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from selectolax.parser import HTMLParser, Node

from ..models import TRACKING_PARAM

_REDIRECT_KEYS = ("url", "u", "q", "target", "dest", "destination", "redirect", "redirect_url", "redirecturl",
                  "link", "l", "goto", "to")
# Link texts that are never a job title.
_GENERIC = re.compile(
    r"^(view( job)?|see (all|more)( jobs)?|ver( la)? oferta|ver (todas|más)( las ofertas)?|apply( now)?|"
    r"postular(me)?|inscribirme|inscr[ií]bete|voir( l'offre| plus)?|postuler|more|m[aá]s|unsubscribe|"
    r"darse de baja|se d[ée]sinscrire|manage alerts?|gestionar alertas?|g[ée]rer .*|privacy.*|"
    r"privacidad.*|help|ayuda|aide|job alert.*|alerta.*|linkedin|indeed|infojobs|efinancialcareers|jobup.*|"
    r"jobs\.ch|michael page|\d+ (new )?jobs?.*|\d+ (nuevas )?ofertas?.*)$",
    re.I,
)

# Links that must never be followed (following an unsubscribe link would cancel the alert).
# Words chosen so that they never appear in job titles ("Wealth Manager" must stay followable).
_ACCOUNT = (r"unsub|opt.?out|darse de baja|dar de baja|suscrip|subscription|d[ée]sabonn|d[ée]sinscri|abmeld|"
            r"abbestell|preferenc|settings|configuraci[oó]n|param[eè]tres|einstellungen|privacy|privacidad|"
            r"confidentialit|datenschutz|login|log in|sign.?in|password|contrase[nñ]a|feedback")
_NEVER_FOLLOW_TEXT = re.compile(_ACCOUNT + r"|(manage|gestionar|g[ée]rer|cancel(ar)?) .*(alert|email|mail)", re.I)
_NEVER_FOLLOW_URL = re.compile(_ACCOUNT + r"|psettings|/baja", re.I)


@dataclass(frozen=True)
class SiteParser:
    name: str
    senders: tuple[str, ...]
    id_pattern: str
    canonical: str | None = None  # template with {id} and {host}; default: URL without query string

    def job_id(self, url: str) -> str | None:
        m = re.search(self.id_pattern, url)
        return m.group(1) if m else None

    def canonical_url(self, url: str, job_id: str) -> str:
        parts = urlsplit(url)
        if self.canonical:
            return self.canonical.format(id=job_id, host=parts.netloc)
        return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, "", ""))


PARSERS: dict[str, SiteParser] = {p.name: p for p in (
    SiteParser("linkedin", ("linkedin.com",), r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d{6,})",
               "https://www.linkedin.com/jobs/view/{id}/"),
    SiteParser("indeed", ("indeed.com", "indeed.es", "indeed.ch", "indeed.fr", "indeedemail.com"),
               r"indeed\.[a-z.]+/.*[?&](?:jk|vjk)=([0-9a-f]{16})", "https://{host}/viewjob?jk={id}"),
    SiteParser("infojobs", ("infojobs.net",), r"infojobs\.net/.*of-i([0-9a-f]{20,40})"),
    SiteParser("efinancialcareers", ("efinancialcareers",), r"efinancialcareers\.[a-z.]+/.*\.id(\d{5,})"),
    SiteParser("jobup", ("jobup.ch", "jobs.ch", "jobcloud.ch"),
               r"(?:jobup|jobs)\.ch/.*/detail/([0-9a-f]{8}-[0-9a-f-]{27,})"),
    SiteParser("michaelpage", ("michaelpage", "pagegroup", "pagepersonnel", "pageexecutive"),
               r"(?:michaelpage|pagepersonnel|pageexecutive)\.[a-z.]+/job-detail/.*?/ref/([\w-]+)"),
)}


@dataclass
class ParsedJob:
    title: str
    url: str
    job_id: str
    company: str = ""
    location: str = ""
    extra_lines: list[str] = field(default_factory=list)


def unwrap(url: str, depth: int = 4) -> str:
    """Follow click-tracking wrappers that carry the target URL in a query parameter."""
    for _ in range(depth):
        params = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        target = next((params[k] for k in params if k.lower() in _REDIRECT_KEYS
                       and unquote(params[k]).startswith(("http://", "https://"))), None)
        if not target:
            break
        url = unquote(target) if "%2F" in target.upper() else target
    return url


def strip_tracking(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAM.match(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def links(html: str) -> list[tuple[str, str, Node]]:
    """(href, text, node) of every link in an HTML e-mail, in order."""
    out = []
    for a in HTMLParser(html).css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if href.startswith(("http://", "https://")):
            out.append((href, " ".join(a.text(separator=" ").split()), a))
    return out


def _is_title(text: str) -> bool:
    return 3 <= len(text) <= 150 and not _GENERIC.match(text.strip(" .:›>»"))


def _block_lines(anchor: Node, parser: SiteParser, job_id: str) -> list[str]:
    """Text lines of the largest block around the link that mentions no other job."""
    node, best = anchor, anchor
    for _ in range(8):
        parent = node.parent
        if parent is None or parent.tag in ("body", "html"):
            break
        ids = {parser.job_id(unwrap(a.attributes.get("href") or "")) for a in parent.css("a[href]")}
        if len(ids - {None, job_id}) > 0:
            break
        best = node = parent
    lines = [" ".join(x.split()) for x in best.text(separator="\n").split("\n")]
    return [x for x in lines if x]


def _split_company_location(lines: list[str], title: str) -> tuple[str, str, list[str]]:
    rest = [x for x in lines if x != title and _is_title(x) and len(x) <= 120]
    if not rest:
        return "", "", []
    first = rest[0]
    for sep in (" · ", " • ", " | ", " – ", " - "):
        if sep in first:
            company, location = first.split(sep, 1)
            return company.strip(), location.strip(), rest[1:]
    return first, rest[1] if len(rest) > 1 else "", rest[2:]


def parse_known(html: str, parser: SiteParser) -> list[ParsedJob]:
    """Jobs in an alert e-mail from a known site (no network, no AI)."""
    found: dict[str, ParsedJob] = {}
    for href, text, node in links(html):
        target = unwrap(href)
        job_id = parser.job_id(target)
        if not job_id:
            continue
        current = found.get(job_id)
        if current and (current.title or not _is_title(text)):
            continue
        lines = _block_lines(node, parser, job_id)
        # Image or "view job" link: the first title-like line of the block is the title.
        title = text if _is_title(text) else next((x for x in lines if _is_title(x)), "")
        if not title:
            continue
        company, location, extra = _split_company_location(lines, title)
        found[job_id] = ParsedJob(title, parser.canonical_url(target, job_id), job_id, company, location, extra)
    return [j for j in found.values() if j.title]


def opaque_links(html: str, parser: SiteParser) -> list[tuple[str, str]]:
    """(href, text) of title-like links whose target is hidden behind an opaque redirect.

    Account links (unsubscribe, settings, privacy, login...) are never returned: they must not be followed.
    """
    out = []
    for href, text, _ in links(html):
        if _NEVER_FOLLOW_TEXT.search(text) or _NEVER_FOLLOW_URL.search(unwrap(href)):
            continue
        if _is_title(text) and not parser.job_id(unwrap(href)):
            out.append((href, text))
    return out


def parser_for(sender: str, enabled: list[str], overrides: dict[str, list[str]] | None = None) -> SiteParser | None:
    sender = sender.lower()
    for name in enabled:
        parser = PARSERS.get(name)
        if parser is None:
            continue
        patterns = (overrides or {}).get(name) or parser.senders
        if any(p.lower() in sender for p in patterns):
            return parser
    return None
