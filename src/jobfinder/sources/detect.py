"""Resolve a configured source to an adapter type and its parameters (auto-detecting known URLs)."""

from __future__ import annotations

from typing import Any

from ..config.schema import Source
from .base import REGISTRY, AdapterError

# Order matters only for URLs several adapters could claim.
DETECT_ORDER = ["workday", "oracle_hcm", "eightfold", "greenhouse", "lever", "smartrecruiters", "ashby",
                "workable", "recruitee", "teamtailor", "personio", "successfactors", "brassring"]


def detect_url(url: str) -> tuple[str, dict[str, Any]] | None:
    names = DETECT_ORDER + [n for n in REGISTRY if n not in DETECT_ORDER]
    for name in names:
        cls = REGISTRY.get(name)
        if cls is None:
            continue
        params = cls.detect(url)
        if params is not None:
            return name, params
    return None


def resolve(source: Source) -> tuple[str, dict[str, Any]]:
    params = source.params()
    if source.url:
        params.setdefault("url", source.url)
    if source.type:
        if source.url and source.type in REGISTRY:
            detected = REGISTRY[source.type].detect(source.url)
            if detected:
                params = {**detected, **params}
        return source.type, params
    if not source.url:
        raise AdapterError("source needs 'url' or 'type'")
    found = detect_url(source.url)
    if not found:
        raise AdapterError("could not detect the site type from the URL; set 'type' explicitly")
    name, detected = found
    return name, {**detected, **params}
