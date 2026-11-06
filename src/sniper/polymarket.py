"""Read-only clients for Polymarket public APIs (Data API v2, Gamma, CLOB market data)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any

from sniper.config import Settings
from sniper.http import ApiClient

log = logging.getLogger(__name__)


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def _gather_chunks(
    chunks: list[list[str]], fetch: Callable[[list[str]], Awaitable[list[dict]]], *, concurrency: int, label: str
) -> list[dict]:
    """Run chunk fetches concurrently (rate limits are still enforced by the client's token buckets)."""
    sem = asyncio.Semaphore(concurrency)
    out: list[dict] = []
    done = 0

    async def one(chunk: list[str]) -> None:
        nonlocal done
        async with sem:
            out.extend(await fetch(chunk))
            done += 1
            if done % 500 == 0 or done == len(chunks):
                log.info("%s: %d/%d requests", label, done, len(chunks))

    await asyncio.gather(*(one(c) for c in chunks))
    return out


# Published per-IP limits (requests per 10 s), docs.polymarket.com/api-reference/rate-limits, 2026-11-06.
DATA_API_GENERAL = 800
DATA_API_ROUTES = {"/trades": 300, "/positions": 200, "/activity": 200, "/prices-history": 200, "/status": 100}
GAMMA_GENERAL = 4000
GAMMA_ROUTES = {"/markets": 300, "/events": 500, "/public-search": 350}
CLOB_GENERAL = 9000
CLOB_ROUTES = {"/prices-history": 1000, "/book": 1500, "/price": 1500}


def _scaled(limits: dict[str, int], share: float) -> dict[str, float]:
    return {k: v * share for k, v in limits.items()}


class DataApi:
    """https://data-api.polymarket.com/v2 (no auth, cursor pagination)."""

    def __init__(self, client: ApiClient):
        self.client = client

    @classmethod
    def create(cls, settings: Settings) -> DataApi:
        return cls(
            ApiClient(
                settings.data_api_url,
                general_limit=DATA_API_GENERAL * settings.rate_limit_share,
                route_limits=_scaled(DATA_API_ROUTES, settings.rate_limit_share),
                timeout_s=settings.http_timeout_s,
                user_agent=settings.user_agent,
            )
        )

    async def leaderboard(
        self, *, time_period: str = "month", category: str = "overall", sort_by: str = "PNL", limit: int = 100
    ) -> list[dict]:
        params = {"time_period": time_period, "category": category, "sort_by": sort_by, "limit": min(limit, 1000)}
        return [row async for row in self.client.paginate("/leaderboard", params, max_items=limit)]

    def activity(
        self,
        user: str,
        *,
        start: int | None = None,
        end: int | None = None,
        max_items: int | None = None,
    ) -> AsyncIterator[dict]:
        """All activity types (TRADE incl. maker fills, SPLIT, MERGE, REDEEM, CONVERSION, rewards, rebates)."""
        params = {"user": user, "start": start, "end": end, "limit": 1000}
        return self.client.paginate("/activity", params, max_items=max_items)

    def trades(
        self,
        *,
        user: str | None = None,
        condition: str | None = None,
        start: int | None = None,
        end: int | None = None,
        max_items: int | None = None,
    ) -> AsyncIterator[dict]:
        # taker_only defaults to true server-side, which hides maker fills.
        params = {"user": user, "condition": condition, "start": start, "end": end, "taker_only": False, "limit": 1000}
        return self.client.paginate("/trades", params, max_items=max_items)

    async def tokens(self, token_ids: Iterable[str], *, concurrency: int = 8) -> list[dict]:
        async def fetch(chunk: list[str]) -> list[dict]:
            return (await self.client.get("/tokens", {"token_id": chunk})).get("data") or []

        return await _gather_chunks(
            list(_chunks(sorted(set(token_ids)), 50)), fetch, concurrency=concurrency, label="tokens"
        )

    async def user_pnl(self, user: str, *, interval: str = "1m", fidelity: str = "1d") -> dict[str, Any]:
        body = await self.client.get("/user-pnl", {"user": user, "interval": interval, "fidelity": fidelity})
        return body.get("data") or {}

    async def user_stats(self, user: str) -> dict[str, Any]:
        body = await self.client.get("/user-stats", {"user": user})
        return body.get("data") or {}

    async def aclose(self) -> None:
        await self.client.aclose()


class GammaApi:
    """https://gamma-api.polymarket.com (market metadata, fee schedules, categories)."""

    def __init__(self, client: ApiClient):
        self.client = client

    @classmethod
    def create(cls, settings: Settings) -> GammaApi:
        return cls(
            ApiClient(
                settings.gamma_url,
                general_limit=GAMMA_GENERAL * settings.rate_limit_share,
                route_limits=_scaled(GAMMA_ROUTES, settings.rate_limit_share),
                timeout_s=settings.http_timeout_s,
                user_agent=settings.user_agent,
            )
        )

    async def markets_by_condition(
        self, condition_ids: Iterable[str], *, closed: bool | None = None, concurrency: int = 8
    ) -> list[dict]:
        """Gamma defaults to closed=false since 2026-04, so the closed flag must be explicit.

        Pass `closed` when it is known (from token metadata) to halve the requests; None queries both.
        """
        states = (True, False) if closed is None else (closed,)

        async def fetch(chunk: list[str]) -> list[dict]:
            rows: list[dict] = []
            for state in states:
                params: list[tuple[str, Any]] = [("condition_ids", c) for c in chunk]
                params += [("closed", state), ("limit", 100)]
                rows.extend(await self.client.get("/markets", params) or [])
            return rows

        chunks = list(_chunks(sorted(set(condition_ids)), 50))
        return await _gather_chunks(chunks, fetch, concurrency=concurrency, label="gamma markets")

    async def closed_markets(
        self, *, end_date_min: str, end_date_max: str, limit: int, order: str = "volumeNum"
    ) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while len(out) < limit:
            page = await self.client.get(
                "/markets",
                {
                    "closed": True,
                    "end_date_min": end_date_min,
                    "end_date_max": end_date_max,
                    "order": order,
                    "ascending": False,
                    "limit": min(100, limit - len(out)),
                    "offset": offset,
                },
            )
            if not page:
                break
            out.extend(page)
            offset += len(page)
        return out

    async def aclose(self) -> None:
        await self.client.aclose()


class ClobApi:
    """https://clob.polymarket.com public market data. Keeps ~1-minute price history far longer than Data API v2."""

    def __init__(self, client: ApiClient):
        self.client = client

    @classmethod
    def create(cls, settings: Settings) -> ClobApi:
        return cls(
            ApiClient(
                settings.clob_url,
                general_limit=CLOB_GENERAL * settings.rate_limit_share,
                route_limits=_scaled(CLOB_ROUTES, settings.rate_limit_share),
                timeout_s=settings.http_timeout_s,
                user_agent=settings.user_agent,
            )
        )

    async def prices_history(
        self, token_id: str, *, start: int, end: int, fidelity_min: int = 1
    ) -> list[tuple[int, float]]:
        body = await self.client.get(
            "/prices-history", {"market": token_id, "startTs": start, "endTs": end, "fidelity": fidelity_min}
        )
        return [(int(p["t"]), float(p["p"])) for p in (body or {}).get("history") or []]

    async def aclose(self) -> None:
        await self.client.aclose()
