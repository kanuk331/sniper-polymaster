"""Async HTTP plumbing shared by the Polymarket API clients: rate limiting, retries, cursor pagination."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Polymarket publishes its limits per 10-second window.
WINDOW_S = 10.0

Params = Mapping[str, Any] | Sequence[tuple[str, Any]]


class ApiError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")
        self.status = status
        self.url = url
        self.body = body


class TokenBucket:
    """Continuous-refill token bucket; capacity = requests allowed per window."""

    def __init__(self, per_window: float, window_s: float = WINDOW_S):
        self.capacity = max(1.0, float(per_window))
        self.rate = self.capacity / window_s
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


def encode_params(params: Params | None) -> list[tuple[str, str]]:
    """Drop None values, render booleans as true/false and lists as comma-separated strings."""
    if params is None:
        return []
    items = params.items() if isinstance(params, Mapping) else params
    out: list[tuple[str, str]] = []
    for key, value in items:
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, list | tuple | set | frozenset):
            value = ",".join(str(v) for v in value)
        out.append((key, str(value)))
    return out


class ApiClient:
    """GET-only JSON client with per-route token buckets and retry on 429/5xx/transport errors."""

    def __init__(
        self,
        base_url: str,
        *,
        general_limit: float,
        route_limits: Mapping[str, float] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 6,
        backoff_base_s: float = 1.0,
        user_agent: str = "sniper-polymaster",
    ):
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=timeout_s, headers={"User-Agent": user_agent}
        )
        self._owns_client = client is None
        self._general = TokenBucket(general_limit)
        self._routes = {prefix: TokenBucket(limit) for prefix, limit in (route_limits or {}).items()}
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.requests_made = 0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> ApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _buckets(self, path: str) -> list[TokenBucket]:
        matches = [p for p in self._routes if path.startswith(p)]
        route = [self._routes[max(matches, key=len)]] if matches else []
        return [self._general, *route]

    def _backoff(self, attempt: int) -> float:
        return min(60.0, self.backoff_base_s * 2**attempt) * (0.5 + random.random() / 2)

    async def get(self, path: str, params: Params | None = None) -> Any:
        query = encode_params(params)
        for attempt in range(self.max_retries + 1):
            for bucket in self._buckets(path):
                await bucket.acquire()
            last = attempt == self.max_retries
            try:
                resp = await self._client.get(path, params=query)
            except httpx.TransportError as exc:
                if last:
                    raise
                delay = self._backoff(attempt)
                log.warning("transport error on %s (%s), retry in %.1fs", path, exc, delay)
                await asyncio.sleep(delay)
                continue
            self.requests_made += 1
            if resp.status_code == 429 or resp.status_code >= 500:
                if last:
                    raise ApiError(resp.status_code, str(resp.url), resp.text)
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after is not None else self._backoff(attempt)
                except ValueError:
                    delay = self._backoff(attempt)
                log.warning("HTTP %s on %s, retry in %.1fs", resp.status_code, path, delay)
                await asyncio.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise ApiError(resp.status_code, str(resp.url), resp.text)
            return resp.json()
        raise AssertionError("unreachable")

    async def paginate(
        self, path: str, params: Mapping[str, Any] | None = None, *, max_items: int | None = None
    ) -> AsyncIterator[dict]:
        """Iterate `data` rows of a Data API v2 `{data, pagination}` envelope, following `next_cursor`.

        v2 feeds require the same filters on every page, so the original params are resent with the cursor.
        """
        query = dict(params or {})
        produced = 0
        while True:
            body = await self.get(path, query)
            for row in body.get("data") or []:
                yield row
                produced += 1
                if max_items is not None and produced >= max_items:
                    return
            page = body.get("pagination") or {}
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                return
            query["cursor"] = cursor
