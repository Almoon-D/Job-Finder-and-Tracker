"""Adapter interface and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from ..config.schema import Config, Source
from ..matching.location import LocationMatcher
from ..models import Job
from ..state import State
from .http import Http

REGISTRY: dict[str, type[Adapter]] = {}


def register(cls: type[Adapter]) -> type[Adapter]:
    REGISTRY[cls.type_name] = cls
    return cls


@dataclass
class FetchContext:
    http: Http
    config: Config
    source: Source
    state: State
    now: datetime
    locations: LocationMatcher
    max_age_days: float = 3.0


class AdapterError(Exception):
    pass


class Adapter(ABC):
    """Fetches jobs from one kind of site.

    Subclasses implement ``fetch`` and optionally ``enrich`` (download the full
    description for a job that survived the cheap filters) and ``detect``
    (recognise their URLs so users can just paste a careers link).
    """

    type_name: ClassVar[str]
    supports_search: ClassVar[bool] = False
    needs_browser: ClassVar[bool] = False

    def __init__(self, ctx: FetchContext, params: dict[str, Any]):
        self.ctx = ctx
        self.source = ctx.source
        self.params = params
        self.http = ctx.http

    @abstractmethod
    async def fetch(self) -> list[Job]: ...

    async def enrich(self, job: Job) -> None:  # noqa: B027 - optional hook
        """Fill in job.description (and possibly locations/dates). Default: nothing."""

    @classmethod
    def detect(cls, url: str) -> dict[str, Any] | None:
        """Return adapter params if ``url`` belongs to this adapter."""
        return None

    # ----------------------------------------------------------- helpers
    def job(self, native_id: str, title: str, url: str, **kwargs: Any) -> Job:
        return Job(
            source_key=self.source.key,
            native_id=str(native_id),
            title=(title or "").strip(),
            url=url,
            company=kwargs.pop("company", None) or self.source.display_company,
            source_type=self.type_name,
            **kwargs,
        )

    def search_terms(self) -> list[str]:
        """Queries run with the location filter (source.queries)."""
        return list(self.source.queries)

    def coverage_terms(self) -> list[str]:
        """Global keyword searches (no location filter) for 'coverage' roles."""
        cov = self.ctx.config.coverage
        if not (self.supports_search and self.source.use_coverage_search and cov.enabled):
            return []
        return list(cov.search_terms)


def build_adapter(ctx: FetchContext) -> Adapter:
    from .detect import resolve

    type_name, params = resolve(ctx.source)
    cls = REGISTRY.get(type_name)
    if cls is None:
        raise AdapterError(f"unknown source type {type_name!r}")
    return cls(ctx, params)
