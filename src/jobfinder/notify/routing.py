"""Which chat gets which jobs (``notify.routing``).

A notification is split when it is sent, never earlier: the pipeline still sees one job with one key (so the
same offer in two chats is one offer for the tracker) and "already notified" stays per group.

* ``summary``    – a compact index of every job, plus the health report; the weekly report goes only here.
* places         – the jobs of each mapped place, in the group's own format (cards, digest...).
* ``highlights`` – jobs from favourite sources or with a high AI fit, also posted in their place chat.
* ``other``      – jobs without a mapped place (and the fallback chat). Without a name: the default chat.

A chat is a name (``None`` is the default chat: DISCORD_CHANNEL_ID / the group's General topic); the channels
turn names into ids (see ``destinations.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from .. import log
from ..config.schema import Config
from ..models import Job, normalize_text
from .base import Notification

SUMMARY_MAX_JOBS = 200


@dataclass
class Plan:
    route: str | None  # chat name; None = the default chat
    kind: str  # "all" (not routed) | "summary" | "highlights" | "place" | "other"
    notification: Notification


class Router:
    def __init__(self, config: Config):
        self.routing = config.notify.routing
        self.favorites = {s.key for s in config.sources if s.favorite}
        self.places = {normalize_text(place): route for place, route in self.routing.places.items()}

    @property
    def active(self) -> bool:
        return self.routing.active

    def is_highlight(self, job: Job) -> bool:
        if job.source_key in self.favorites:
            return True
        # The copy that was kept can come from another source than the favourite that also found it.
        if any(d.source_key in self.favorites for d in job.extra.get("duplicates", [])):
            return True
        return job.score is not None and job.score >= self.routing.highlight_score

    def plan(self, n: Notification) -> list[Plan]:
        """The messages to send for ``n``, in sending order. Not active: ``n`` itself, once, to the default chat."""
        r = self.routing
        if not r.active:
            return [Plan(None, "all", n)]
        if n.test:
            names = r.routes() + ([None] if not r.other else [])
            return [Plan(name, "summary" if name == r.summary else "other",
                         replace(n, buttons=n.buttons and name != r.summary)) for name in names]
        if n.report is not None or n.empty:
            # No news, or the weekly report: one message, in the chat that keeps the overview.
            return [Plan(r.summary or r.other, "summary" if r.summary else "other", n)]

        plans: list[Plan] = []
        if r.summary:
            plans.append(Plan(r.summary, "summary", replace(
                n, jobs=list(n.jobs), format="grouped", buttons=False, silent=True,
                max_items=min(len(n.jobs), SUMMARY_MAX_JOBS), problems=list(n.problems))))

        cards: dict[str | None, tuple[str, list[Job]]] = {}  # chat -> (kind, jobs); a chat never gets a job twice

        def add(route: str | None, kind: str, job: Job) -> None:
            jobs = cards.setdefault(route, (kind, []))[1]
            if all(j.key != job.key for j in jobs):
                jobs.append(job)

        for job in n.jobs:
            if r.highlights and self.is_highlight(job):
                add(r.highlights, "highlights", job)
        for job in n.jobs:
            place = normalize_text(job.extra.get("target") or "")
            route = self.places.get(place) if place else None
            if route:
                add(route, "place", job)
            else:
                add(r.other, "other", job)

        # Health warnings travel with the overview; without a Resumen, with the catch-all chat.
        carrier = None if r.summary else r.other
        if not r.summary and n.problems and carrier not in cards:
            cards[carrier] = ("other", [])  # every job has its place: the warnings still need a chat
        for route, (kind, jobs) in cards.items():
            carries = not r.summary and route == carrier
            highlights = kind == "highlights"
            plans.append(Plan(route, kind, replace(
                n, jobs=jobs, format="per_job" if highlights else n.format,
                priority="high" if highlights else n.priority,
                problems=list(n.problems) if carries else [],
                show_sources=carries, also_elsewhere=n.also_elsewhere if carries else 0)))
        return plans


async def deliver_plans(channel: str, plans: list[Plan], send_one: Callable[[Plan], Awaitable[None]],
                        test: bool = False, pause: float = 0) -> None:
    """Send every plan; a chat that fails does not stop the others.

    A normal run succeeds when at least one chat got its message (the Resumen index lists every job, so a
    single broken chat loses nothing it does not also show); it fails only when all of them failed. A test
    fails when any chat failed: that is what it is for. Logs hold counts only (chat names are private).
    """
    errors: list[Exception] = []
    for i, plan in enumerate(plans):
        if i and pause:
            await asyncio.sleep(pause)
        try:
            await send_one(plan)
        except Exception as exc:  # noqa: BLE001 - reported below; the other chats still go out
            errors.append(exc)
    if errors and (test or len(errors) == len(plans)):
        raise errors[0]
    if errors:
        log.warn(f"notify/{channel}: {len(errors)} of {len(plans)} chats failed; the others were sent")
