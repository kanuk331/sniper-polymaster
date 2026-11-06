"""CLOB order-book snapshots (`GET /book`) and the execution math paper trading applies to them."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol

import httpx

from sniper.http import ApiClient, ApiError

EPS = 1e-9


class Level(NamedTuple):
    price: float
    size: float  # shares


class Fill(NamedTuple):
    avg_price: float | None  # VWAP of the filled part; None when nothing filled
    shares: float
    worst_price: float | None  # last (worst) level touched
    usdc: float  # notional filled
    complete: bool
    legs: tuple[Level, ...] = ()  # shares taken per level, for per-level taker fees


def _levels(raw: Any) -> list[Level]:
    out: list[Level] = []
    for item in raw or []:
        try:
            if isinstance(item, dict):
                price, size = float(item["price"]), float(item["size"])
            else:
                price, size = float(item[0]), float(item[1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if size > 0 and 0 < price < 1:
            out.append(Level(price, size))
    return out


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@dataclass
class OrderBook:
    token_id: str
    bids: list[Level] = field(default_factory=list)  # best (highest) first
    asks: list[Level] = field(default_factory=list)  # best (lowest) first
    ts_ms: int | None = None  # server time of the last book change, not of the request
    min_order_size: float = 0.0
    tick_size: float | None = None
    neg_risk: bool = False
    last_trade_price: float | None = None
    market: str = ""
    hash: str = ""

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> OrderBook:
        """The API lists bids ascending and asks descending (best level last), with string numbers."""
        ts = _float_or_none(body.get("timestamp"))
        return cls(
            token_id=str(body.get("asset_id") or ""),
            bids=sorted(_levels(body.get("bids")), key=lambda lv: -lv.price),
            asks=sorted(_levels(body.get("asks")), key=lambda lv: lv.price),
            ts_ms=int(ts) if ts is not None else None,
            min_order_size=_float_or_none(body.get("min_order_size")) or 0.0,
            tick_size=_float_or_none(body.get("tick_size")),
            neg_risk=bool(body.get("neg_risk")),
            last_trade_price=_float_or_none(body.get("last_trade_price")),
            market=str(body.get("market") or ""),
            hash=str(body.get("hash") or ""),
        )

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2

    @property
    def spread(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return self.asks[0].price - self.bids[0].price

    def depth_usdc(self, side: str, within: float = 0.02) -> float:
        """USDC notional resting within `within` of the best price on `side` ("bid" or "ask")."""
        if side not in ("bid", "ask"):
            raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")
        levels = self.bids if side == "bid" else self.asks
        if not levels:
            return 0.0
        best = levels[0].price
        if side == "ask":
            return sum(lv.price * lv.size for lv in levels if lv.price <= best + within + EPS)
        return sum(lv.price * lv.size for lv in levels if lv.price >= best - within - EPS)

    def usdc_up_to(self, max_price: float) -> float:
        """Ask notional available at or below `max_price`."""
        return sum(lv.price * lv.size for lv in self.asks if lv.price <= max_price + EPS)

    def vwap_buy(self, usdc: float, max_price: float | None = None) -> Fill:
        """Spend up to `usdc` walking the asks, never paying more than `max_price` per share."""
        remaining = usdc
        shares = cost = 0.0
        worst: float | None = None
        legs: list[Level] = []
        for lv in self.asks:
            if remaining <= EPS:
                break
            if max_price is not None and lv.price > max_price + EPS:
                break
            take = min(remaining, lv.price * lv.size)
            q = take / lv.price
            shares += q
            cost += take
            remaining -= take
            worst = lv.price
            legs.append(Level(lv.price, q))
        complete = remaining <= EPS * max(1.0, usdc)
        return Fill(cost / shares if shares > 0 else None, shares, worst, cost, complete, tuple(legs))

    def vwap_sell(self, shares: float, min_price: float | None = None) -> Fill:
        """Sell up to `shares` walking the bids, never accepting less than `min_price` per share."""
        remaining = shares
        sold = proceeds = 0.0
        worst: float | None = None
        legs: list[Level] = []
        for lv in self.bids:
            if remaining <= EPS:
                break
            if min_price is not None and lv.price < min_price - EPS:
                break
            q = min(remaining, lv.size)
            sold += q
            proceeds += q * lv.price
            remaining -= q
            worst = lv.price
            legs.append(Level(lv.price, q))
        complete = remaining <= EPS * max(1.0, shares)
        return Fill(proceeds / sold if sold > 0 else None, sold, worst, proceeds, complete, tuple(legs))

    def top_levels(self, n: int = 10) -> dict[str, list[list[float]]]:
        return {
            "bids": [[lv.price, lv.size] for lv in self.bids[:n]],
            "asks": [[lv.price, lv.size] for lv in self.asks[:n]],
        }


@dataclass
class BookSnapshot:
    token_id: str
    book: OrderBook | None
    req_start_ms: int
    req_end_ms: int
    error: str | None = None

    @property
    def latency_ms(self) -> int:
        return self.req_end_ms - self.req_start_ms

    @property
    def observed_at(self) -> float:
        """When the book state was observed (response received), epoch seconds."""
        return self.req_end_ms / 1000


async def fetch_book(client: ApiClient, token_id: str, *, clock: Callable[[], float] = time.time) -> BookSnapshot:
    """GET /book with request timing; failures (404 for closed markets, timeouts) become `error`."""
    start = clock()
    book: OrderBook | None = None
    error: str | None = None
    try:
        body = await client.get("/book", {"token_id": token_id})
        if not isinstance(body, dict):
            raise ValueError("unexpected /book response")
        book = OrderBook.from_api(body)
        book.token_id = book.token_id or token_id
    except ApiError as exc:
        error = f"HTTP {exc.status}: {exc.body[:120]}"
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        error = f"{type(exc).__name__}: {str(exc)[:120]}"
    return BookSnapshot(token_id, book, int(start * 1000), int(clock() * 1000), error)


class BookSource(Protocol):
    async def fetch(self, token_id: str) -> BookSnapshot: ...


class ClobBooks:
    """BookSource over a CLOB ApiClient; build the client with few retries so timing stays honest."""

    def __init__(self, client: ApiClient, *, clock: Callable[[], float] = time.time):
        self.client = client
        self.clock = clock
        self.fetched = 0
        self.failed = 0

    async def fetch(self, token_id: str) -> BookSnapshot:
        snap = await fetch_book(self.client, token_id, clock=self.clock)
        self.fetched += 1
        self.failed += snap.book is None
        return snap
