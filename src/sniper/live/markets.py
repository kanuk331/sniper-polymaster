"""Token and market metadata for live signals: category, fee schedule, event key, resolution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sniper.analytics import event_key_for
from sniper.db import category_from_fee_type
from sniper.polymarket import DataApi, GammaApi

log = logging.getLogger(__name__)

# Used only when metadata lookups fail: the "other" category rate, so fees are not silently zero.
FALLBACK_FEE_RATE = 0.05


@dataclass(frozen=True)
class MarketInfo:
    token_id: str
    condition_id: str
    category: str = "unknown"
    fee_rate: float = 0.0  # market schedule rate; fees.taker_fee_rate applies the regime in force
    event_key: str = ""
    question: str = ""
    market_slug: str = ""
    event_slug: str = ""
    end_date: str = ""
    neg_risk: bool = False
    resolved: bool = False
    final_price: float | None = None
    complete: bool = True  # False when built from fallbacks

    @classmethod
    def fallback(cls, token_id: str, condition_id: str) -> MarketInfo:
        return cls(token_id, condition_id, fee_rate=FALLBACK_FEE_RATE, event_key=f"c:{condition_id}", complete=False)


def info_from(token_id: str, condition_id: str, token: dict[str, Any] | None, market: dict[str, Any] | None) -> MarketInfo:
    token = token or {}
    cid = str(token.get("condition_id") or condition_id or (market or {}).get("conditionId") or "").lower()
    if market is None:
        base = MarketInfo.fallback(token_id, cid)
    else:
        fee = market.get("feeSchedule") or {}
        base = MarketInfo(
            token_id,
            cid,
            category=category_from_fee_type(market.get("feeType")),
            fee_rate=float(fee.get("rate") or 0.0),
            question=str(market.get("question") or ""),
            market_slug=str(market.get("slug") or ""),
            end_date=str(market.get("endDate") or ""),
            neg_risk=bool(market.get("negRisk")),
        )
    events = (market or {}).get("events") or [{}]
    key_row = {
        "neg_risk": token.get("neg_risk", base.neg_risk),
        "neg_risk_market_id": token.get("neg_risk_market_id") or (market or {}).get("negRiskMarketID"),
        "event_id": token.get("event_id") or events[0].get("id"),
        "condition_id": cid,
    }
    final = token.get("final_price")
    return MarketInfo(
        token_id=token_id,
        condition_id=cid,
        category=base.category,
        fee_rate=base.fee_rate,
        event_key=event_key_for(key_row),
        question=base.question or str(token.get("title") or ""),
        market_slug=base.market_slug or str(token.get("market_slug") or ""),
        event_slug=str(token.get("event_slug") or events[0].get("slug") or ""),
        end_date=base.end_date,
        neg_risk=bool(key_row["neg_risk"]),
        resolved=bool(token.get("resolved")),
        final_price=float(final) if final is not None else None,
        complete=base.complete and bool(token),
    )


class MarketDirectory:
    """Cached token -> MarketInfo lookups (Data API /tokens + Gamma /markets); concurrent callers share one fetch."""

    def __init__(self, data_api: DataApi | None, gamma: GammaApi | None):
        self.data_api = data_api
        self.gamma = gamma
        self._cache: dict[str, MarketInfo] = {}
        self._inflight: dict[str, asyncio.Task[MarketInfo]] = {}

    async def get(self, token_id: str, condition_id: str = "") -> MarketInfo:
        cached = self._cache.get(token_id)
        if cached is not None:
            return cached
        task = self._inflight.get(token_id)
        if task is None:
            task = asyncio.create_task(self._load(token_id, condition_id))
            self._inflight[token_id] = task
            task.add_done_callback(lambda _t: self._inflight.pop(token_id, None))
        return await asyncio.shield(task)

    async def _tokens(self, token_ids: list[str]) -> list[dict[str, Any]]:
        if self.data_api is None:
            return []
        out: list[dict[str, Any]] = []
        for i in range(0, len(token_ids), 50):
            body = await self.data_api.client.get("/tokens", {"token_id": token_ids[i : i + 50]})
            out.extend((body or {}).get("data") or [])
        return out

    async def _market(self, condition_id: str, closed: bool | None) -> dict[str, Any] | None:
        if self.gamma is None or not condition_id:
            return None
        for state in (closed,) if closed is not None else (False, True):
            params = [("condition_ids", condition_id), ("closed", state), ("limit", 10)]
            for m in await self.gamma.client.get("/markets", params) or []:
                if str(m.get("conditionId") or "").lower() == condition_id.lower():
                    return m
        return None

    async def _load(self, token_id: str, condition_id: str) -> MarketInfo:
        token: dict[str, Any] | None = None
        market: dict[str, Any] | None = None
        try:
            rows = await self._tokens([token_id])
            token = next((r for r in rows if str(r.get("token_id")) == token_id), None)
        except Exception as exc:  # metadata is best effort; the signal must not die with it
            log.warning("token lookup failed for %s: %s", token_id[:12], exc)
        cid = str((token or {}).get("condition_id") or condition_id)
        try:
            market = await self._market(cid, bool(token.get("closed")) if token else None)
        except Exception as exc:
            log.warning("gamma lookup failed for %s: %s", cid[:12], exc)
        info = info_from(token_id, cid, token, market)
        if info.complete:
            self._cache[token_id] = info
        return info

    async def resolutions(self, token_ids: Iterable[str]) -> dict[str, float]:
        """token -> final price for the resolved ones."""
        rows = await self._tokens(sorted(set(token_ids)))
        out: dict[str, float] = {}
        for r in rows:
            if r.get("resolved") and r.get("final_price") is not None:
                out[str(r["token_id"])] = float(r["final_price"])
        return out
