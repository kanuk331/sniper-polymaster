"""Collapse RTDS fills into leader orders (one signal per transaction, wallet, token and side)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sniper.live.rtds import TradeEvent

log = logging.getLogger(__name__)

OrderKey = tuple[str, str, str, str]  # (tx_hash, wallet, token_id, side)


@dataclass
class LeaderOrder:
    tx_hash: str
    wallet: str
    token_id: str
    side: str
    condition_id: str = ""
    outcome: str = ""
    outcome_index: int = 0
    slug: str = ""
    event_slug: str = ""
    title: str = ""
    name: str = ""
    size: float = 0.0
    notional: float = 0.0
    fee: float = 0.0
    worst_price: float = 0.0  # highest fill price for BUY, lowest for SELL
    n_fills: int = 0
    first_ts: int = 0  # leader trade time (feed payload), epoch seconds
    last_ts: int = 0
    first_received: float = 0.0  # local time the first fill arrived
    feed_ts_ms: int | None = None
    detected_at: float = 0.0  # local time the aggregated order was emitted

    @classmethod
    def from_event(cls, ev: TradeEvent) -> LeaderOrder:
        order = cls(
            tx_hash=ev.tx_hash,
            wallet=ev.wallet,
            token_id=ev.token_id,
            side=ev.side,
            condition_id=ev.condition_id,
            outcome=ev.outcome,
            outcome_index=ev.outcome_index,
            slug=ev.slug,
            event_slug=ev.event_slug,
            title=ev.title,
            name=ev.name,
            first_ts=ev.ts,
            last_ts=ev.ts,
            first_received=ev.received_at,
            feed_ts_ms=ev.feed_ts_ms,
        )
        order.add(ev)
        return order

    def add(self, ev: TradeEvent) -> None:
        self.n_fills += 1
        self.size += ev.size
        self.notional += ev.notional
        self.fee += ev.fee
        if self.n_fills == 1:
            self.worst_price = ev.price
        else:
            self.worst_price = (
                max(self.worst_price, ev.price) if self.side == "BUY" else min(self.worst_price, ev.price)
            )
        self.first_ts = min(self.first_ts, ev.ts)
        self.last_ts = max(self.last_ts, ev.ts)

    @property
    def key(self) -> OrderKey:
        return (self.tx_hash, self.wallet, self.token_id, self.side)

    @property
    def vwap(self) -> float:
        return self.notional / self.size if self.size else 0.0

    @property
    def is_taker(self) -> bool:
        """Only meaningful on fee-enabled markets: makers pay no fee."""
        return self.fee > 0


class OrderAggregator:
    """Buffers fills per order key and emits each order `window_s` after its first fill arrived.

    `wallet_filter` runs on every fill of the global feed (~35/s), so pass an O(1) check such as
    `frozenset.__contains__`; None keeps every wallet (debug mode).
    """

    def __init__(
        self,
        *,
        window_s: float = 1.5,
        wallet_filter: Callable[[str], bool] | None = None,
        max_open: int = 50_000,
    ):
        self.window_s = window_s
        self.wallet_filter = wallet_filter
        self.max_open = max_open
        self._open: dict[OrderKey, LeaderOrder] = {}  # insertion order == first-fill arrival order
        self.fills_seen = 0
        self.fills_kept = 0
        self.orders_emitted = 0
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._open)

    def add(self, ev: TradeEvent) -> bool:
        self.fills_seen += 1
        if self.wallet_filter is not None and not self.wallet_filter(ev.wallet):
            return False
        key = (ev.tx_hash, ev.wallet, ev.token_id, ev.side)
        order = self._open.get(key)
        if order is not None:
            order.add(ev)
        elif len(self._open) >= self.max_open:
            self.dropped += 1
            return False
        else:
            self._open[key] = LeaderOrder.from_event(ev)
        self.fills_kept += 1
        return True

    def pop_due(self, now: float) -> list[LeaderOrder]:
        out: list[LeaderOrder] = []
        while self._open:
            key = next(iter(self._open))
            order = self._open[key]
            if now - order.first_received < self.window_s:
                break
            del self._open[key]
            order.detected_at = now
            out.append(order)
        self.orders_emitted += len(out)
        return out

    def pop_all(self, now: float) -> list[LeaderOrder]:
        out = list(self._open.values())
        self._open.clear()
        for order in out:
            order.detected_at = now
        self.orders_emitted += len(out)
        return out
