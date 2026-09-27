"""Shared async HTTP client: per-host concurrency, retries and browser-like headers."""

from __future__ import annotations

import asyncio
import random
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config.schema import HttpConfig

RETRY_STATUS = {429, 500, 502, 503, 504, 999}


class HttpError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


class Http:
    def __init__(self, cfg: HttpConfig | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg or HttpConfig()
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": self.cfg.user_agent,
                "Accept-Language": self.cfg.accept_language,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            },
            timeout=self.cfg.timeout_seconds,
            follow_redirects=True,
            http2=transport is None,
            transport=transport,
        )
        self._sems: dict[str, asyncio.Semaphore] = {}

    async def __aenter__(self) -> Http:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _sem(self, url: str) -> asyncio.Semaphore:
        host = urlsplit(url).netloc
        if host not in self._sems:
            self._sems[host] = asyncio.Semaphore(self.cfg.max_per_host)
        return self._sems[host]

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self.cfg.retries + 1):
            try:
                async with self._sem(url):
                    resp = await self._client.request(method, url, **kwargs)
                if resp.status_code in RETRY_STATUS and attempt < self.cfg.retries:
                    retry_after = resp.headers.get("Retry-After", "")
                    delay = float(retry_after) if retry_after.isdigit() else 2 ** (attempt + 1)
                    await asyncio.sleep(min(delay, 30) + random.random())
                    continue
                if resp.status_code >= 400:
                    raise HttpError(resp.status_code, f"HTTP {resp.status_code}")
                return resp
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < self.cfg.retries:
                    await asyncio.sleep(2 ** (attempt + 1) + random.random())
                    continue
                raise HttpError(None, f"network error: {type(exc).__name__}") from exc
        raise HttpError(None, f"request failed: {last_exc!r}")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = await self.request("GET", url, **kwargs)
        return _json(resp)

    async def post_json(self, url: str, json: Any = None, **kwargs: Any) -> Any:
        resp = await self.request("POST", url, json=json, **kwargs)
        return _json(resp)

    async def get_text(self, url: str, **kwargs: Any) -> str:
        resp = await self.request("GET", url, **kwargs)
        return resp.text


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError as exc:
        raise HttpError(resp.status_code, "response is not JSON") from exc
