"""Paper trading: copy tracked leaders' orders against real order-book snapshots, one portfolio per reaction delay.

For every leader BUY order and every delay variant d, the engine snapshots the order book at detection + d and
applies the backtest's copy rules to the real asks. Each (signal, variant) becomes one `metrics.Lot`, filled or
skipped, so `metrics.summarize` / `breakdown` report paper results exactly like backtests.

Entry checks, in order (the first failing one is the skip reason):
- no_book: the snapshot failed (closed market, timeout, HTTP error).
- no_edge: Kelly mode only, no positive stake after fees.
- premium: the best ask is above leader VWAP + max_premium, i.e. the price moved away before the follower acted.
- depth: the best ask is within that cap, but the asks within the cap hold less than stake x min_fill_ratio: the
  book is too thin at acceptable prices (a smaller stake might still fill).
- extreme_price: the follower's VWAP is outside [min_price, max_price].
- min_size: fewer shares than the market's minimum order size.
- paused / risk_*: the variant's risk engine (manual pause, loss stops, kill switch, exposure limits).

Exits mirror the leader. A SELL of fraction f of the leader's holdings (holdings = /positions at start-up plus feed
trades since) sells f of every lot copied from that leader in that token, at detection + d, walking the bids.
Reductions seen only in /positions (merges, transfers) are mirrored the same way once the position has been quiet
on the feed. Resolved markets settle at the final price. The leader benchmark (same stake at the leader's VWAP, no
fees, leader's exit prices) is tracked for every signal, filled or skipped, as in the backtest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sniper.fees import taker_fee, taker_fee_rate
from sniper.live.book import EPS, BookSnapshot, BookSource, Fill
from sniper.live.markets import MarketDirectory, MarketInfo
from sniper.live.risk import Exposure, RiskConfig, RiskDecision, RiskEngine
from sniper.live.signals import LeaderOrder
from sniper.live.store import PaperStore, lot_from_json
from sniper.live.watchlist import WatchEntry
from sniper.metrics import FILLED, Lot

log = logging.getLogger(__name__)

PERIOD = "live"


@dataclass(frozen=True)
class Variant:
    strategy: str
    delay_s: int

    @property
    def name(self) -> str:
        return f"{self.strategy}@{self.delay_s}s"


@dataclass(frozen=True)
class PaperConfig:
    strategy: str = "baseline"
    delays: tuple[int, ...] = (0, 10, 60, 300)
    max_premium: float = 0.015
    min_price: float = 0.03
    max_price: float = 0.97
    min_fill_ratio: float = 1.0  # share of the stake that must fill under the price cap
    depth_within: float = 0.02  # depth columns: notional within this distance of the best price
    book_levels: int = 10  # levels per side kept in snapshots.levels_json
    reconcile_quiet_s: float = 600.0  # /positions reductions count only after this long without feed trades
    reconcile_tolerance: float = 0.01  # relative reduction ignored as noise
    info_timeout_s: float = 15.0
    risk: RiskConfig = field(default_factory=RiskConfig)


@dataclass(eq=False)
class OpenLot:
    lot_id: int
    lot: Lot
    variant: Variant
    exposure: Exposure
    fee_rate: float  # market schedule rate
    seq: int = 0  # order of detection; an exit only touches lots whose leader order was detected before it
    shares: float = 0.0  # follower shares still held
    basis: float = 0.0  # follower cost basis still held
    bench_shares: float = 0.0  # leader-benchmark shares still held

    @property
    def is_open(self) -> bool:
        return self.shares > EPS or self.bench_shares > EPS


@dataclass
class EntryOutcome:
    order: LeaderOrder
    order_id: int
    entry: WatchEntry
    variant: Variant
    lot: Lot
    info: MarketInfo
    snapshot: BookSnapshot | None
    lot_id: int = 0
    stake: float = 0.0  # desired stake before risk limits (0 = no Kelly edge)
    probe_stake: float = 0.0  # stake walked through the book (the nominal stake when `stake` is 0)
    cap: float = 0.0  # max price per share (leader VWAP + max_premium)
    probe: Fill | None = None  # the stake walked through the asks under the cap
    uncapped: Fill | None = None  # the stake walked through the asks without a cap
    risk: RiskDecision | None = None
    usage: dict[str, float] = field(default_factory=dict)


@dataclass
class ExitOutcome:
    variant: Variant
    wallet: str
    token_id: str
    kind: str  # sell | merge
    fraction: float
    status: str = "no_position"  # ok | partial | no_book | no_bids | no_position
    shares: float = 0.0
    proceeds: float = 0.0
    fee: float = 0.0
    basis: float = 0.0
    lots: int = 0
    snapshot: BookSnapshot | None = None

    @property
    def pnl(self) -> float:
        return self.proceeds - self.fee - self.basis


@dataclass
class Settled:
    variant: Variant
    open_lot: OpenLot
    shares: float
    value: float
    basis: float

    @property
    def pnl(self) -> float:
        return self.value - self.basis


class PaperListener:
    """Hooks for alerts; every method is optional."""

    async def entry(self, outcome: EntryOutcome) -> None:
        return None

    async def exit(self, outcome: ExitOutcome, order: LeaderOrder | None) -> None:
        return None

    async def settled(self, token_id: str, final_price: float, closed: list[Settled]) -> None:
        return None

    async def kill_switch(self, variant: Variant, equity: float, peak: float) -> None:
        return None


def _book_stats(snap: BookSnapshot, within: float) -> dict[str, Any]:
    book = snap.book
    if book is None:
        return {"error": snap.error}
    return {
        "best_bid": book.best_bid,
        "best_ask": book.best_ask,
        "mid": book.mid,
        "spread": book.spread,
        "bid_depth_usdc": book.depth_usdc("bid", within),
        "ask_depth_usdc": book.depth_usdc("ask", within),
        "min_order_size": book.min_order_size,
        "tick_size": book.tick_size,
        "book_ts_ms": book.ts_ms,
        "fetch_ms": snap.latency_ms,
    }


class PaperEngine:
    def __init__(
        self,
        store: PaperStore,
        books: BookSource,
        markets: MarketDirectory,
        cfg: PaperConfig | None = None,
        *,
        listener: PaperListener | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.books = books
        self.markets = markets
        self.cfg = cfg or PaperConfig()
        self.listener = listener or PaperListener()
        self.clock = clock
        self.variants = [Variant(self.cfg.strategy, int(d)) for d in sorted(set(self.cfg.delays))]
        self.risk = {v: RiskEngine(self.cfg.risk) for v in self.variants}
        self.positions: dict[Variant, dict[tuple[str, str], list[OpenLot]]] = {v: {} for v in self.variants}
        self.holdings: dict[str, dict[str, float]] = defaultdict(dict)  # leader wallet -> token -> shares
        self.last_activity: dict[tuple[str, str], float] = {}
        self.marks: dict[str, float] = {}  # token -> last observed mid
        self.resolved: dict[str, float] = {}  # token -> final price
        self.titles: dict[str, str] = {}  # token -> "market title · outcome", for messages
        self.counters: Counter[str] = Counter()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pending: dict[tuple[Variant, str, str], set[asyncio.Task[Any]]] = defaultdict(set)
        self._tripped: list[Variant] = []
        self._seq = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @property
    def fastest(self) -> Variant:
        return self.variants[0]

    # --- state helpers ------------------------------------------------------------------------------------------------

    def lots_of(self, v: Variant) -> Iterable[OpenLot]:
        for group in self.positions[v].values():
            yield from group

    def has_position(self, v: Variant, wallet: str, token_id: str) -> bool:
        return bool(self.positions[v].get((wallet, token_id))) or bool(self._pending.get((v, wallet, token_id)))

    def has_any_position(self, wallet: str, token_id: str) -> bool:
        return any(self.has_position(v, wallet, token_id) for v in self.variants)

    def open_tokens(self) -> set[str]:
        """Tokens with open follower or benchmark shares that are not known to be resolved."""
        return {t for v in self.variants for (_, t) in self.positions[v] if t not in self.resolved}

    def held_tokens(self) -> set[str]:
        return {ol.lot.token_id for v in self.variants for ol in self.lots_of(v) if ol.shares > EPS}

    def unrealized(self, v: Variant) -> tuple[float, int, int]:
        """(unrealized PnL at the last observed mids, open lots, lots without a mark: carried at cost)."""
        total, n_open, unmarked = 0.0, 0, 0
        for ol in self.lots_of(v):
            if ol.shares <= EPS:
                continue
            n_open += 1
            mark = self.marks.get(ol.lot.token_id)
            if mark is None:
                unmarked += 1
                continue
            total += ol.shares * mark - ol.basis
        return total, n_open, unmarked

    def equity(self, v: Variant) -> float:
        return self.cfg.risk.bankroll + self.risk[v].realized + self.unrealized(v)[0]

    def _refresh_equity(self, v: Variant) -> None:
        if self.risk[v].update_equity(self.equity(v)):
            self._tripped.append(v)
            self.counters["kill_switch"] += 1
            log.warning("kill switch tripped for %s: equity %.2f", v.name, self.risk[v].equity)
        self.store.set_meta(f"risk:{v.name}", json.dumps(self.risk[v].state()))

    def _save(self, ol: OpenLot) -> None:
        self.store.update_lot(
            ol.lot_id, ol.lot, open_shares=ol.shares, open_basis=ol.basis, bench_shares=ol.bench_shares
        )

    def _prune(self, v: Variant, key: tuple[str, str]) -> None:
        group = [ol for ol in self.positions[v].get(key, []) if ol.is_open]
        if group:
            self.positions[v][key] = group
        else:
            self.positions[v].pop(key, None)

    def _observe(self, snap: BookSnapshot | None) -> None:
        if snap is not None and snap.book is not None and snap.book.mid is not None:
            self.marks[snap.token_id] = snap.book.mid

    def _snapshot_row(
        self,
        snap: BookSnapshot,
        *,
        purpose: str,
        variant: Variant | None = None,
        order_id: int | None = None,
        scheduled: float | None = None,
        leader_ts: int | None = None,
        fill: Fill | None = None,
        levels: bool = True,
    ) -> dict[str, Any]:
        book = snap.book
        stats = _book_stats(snap, self.cfg.depth_within) if book is not None else {}
        return {
            "order_id": order_id,
            "variant": variant.name if variant else None,
            "purpose": purpose,
            "token_id": snap.token_id,
            "scheduled_ms": int(scheduled * 1000) if scheduled is not None else None,
            "req_start_ms": snap.req_start_ms,
            "req_end_ms": snap.req_end_ms,
            "book_ts_ms": book.ts_ms if book else None,
            "leader_ts": leader_ts,
            "best_bid": stats.get("best_bid"),
            "best_ask": stats.get("best_ask"),
            "mid": stats.get("mid"),
            "spread": stats.get("spread"),
            "bid_depth_usdc": stats.get("bid_depth_usdc"),
            "ask_depth_usdc": stats.get("ask_depth_usdc"),
            "vwap": fill.avg_price if fill else None,
            "vwap_shares": fill.shares if fill else None,
            "vwap_usdc": fill.usdc if fill else None,
            "vwap_complete": int(fill.complete) if fill else None,
            "min_order_size": book.min_order_size if book else None,
            "tick_size": book.tick_size if book else None,
            "last_trade_price": book.last_trade_price if book else None,
            "levels_json": json.dumps(book.top_levels(self.cfg.book_levels)) if book and levels else None,
            "error": snap.error,
        }

    # --- leader orders ------------------------------------------------------------------------------------------------

    def record_order(self, order: LeaderOrder, entry: WatchEntry) -> tuple[int | None, float]:
        """Persist the order and update the leader's holdings.

        Returns (order id, exit fraction); the id is None for fills of an order that was already emitted.
        """
        held = self.holdings[order.wallet]
        before = held.get(order.token_id, 0.0)
        self.last_activity[(order.wallet, order.token_id)] = order.detected_at or self.clock()
        if order.title:
            self.titles[order.token_id] = f"{order.title} · {order.outcome}" if order.outcome else order.title
        frac = 0.0
        if order.side == "BUY":
            after = before + order.size
        else:
            after = max(0.0, before - order.size)
            # Unknown holdings (e.g. a restart lost them) with copied lots: mirror as a full exit.
            frac = min(1.0, order.size / before) if before > EPS else 1.0
        if after > EPS:
            held[order.token_id] = after
        else:
            held.pop(order.token_id, None)
        order_id = self.store.insert_order(
            order,
            tier=entry.tier,
            role=entry.role,
            holdings_before=before,
            exit_fraction=frac if order.side == "SELL" else None,
        )
        if order_id is None:
            self.store.add_late_fills(order)
            self.counters["late_orders"] += 1
        return order_id, frac

    def on_order(self, order: LeaderOrder, entry: WatchEntry) -> int | None:
        """Record a leader order and schedule its per-variant entries (BUY) or mirrored exits (SELL)."""
        order_id, frac = self.record_order(order, entry)
        if order_id is None:
            return None
        seq = self.next_seq()
        if order.side == "BUY":
            self.counters["buy_orders"] += 1
            info_task = self._spawn(self.markets.get(order.token_id, order.condition_id))
            for v in self.variants:
                key = (v, order.wallet, order.token_id)
                task = self._spawn(self._entry_task(order, order_id, entry, v, info_task, seq))
                self._pending[key].add(task)
                task.add_done_callback(lambda t, k=key: self._pending_done(k, t))
            return order_id
        self.counters["sell_orders"] += 1
        for v in self.variants:
            self._schedule_exit(
                v, order.wallet, order.token_id, frac, kind="sell", due=order.detected_at + v.delay_s,
                bench_price=order.vwap, seq=seq, order=order, order_id=order_id,
            )  # fmt: skip
        return order_id

    def _schedule_exit(
        self,
        v: Variant,
        wallet: str,
        token_id: str,
        fraction: float,
        *,
        kind: str,
        due: float,
        bench_price: float,
        seq: int,
        order: LeaderOrder | None = None,
        order_id: int | None = None,
    ) -> bool:
        if not self.has_position(v, wallet, token_id):
            return False
        # Entries detected before this exit must be booked first, even if their snapshot is slower.
        earlier = list(self._pending.get((v, wallet, token_id), ()))
        self._spawn(
            self._exit_task(
                v,
                wallet,
                token_id,
                fraction,
                kind=kind,
                due=due,
                bench_price=bench_price,
                seq=seq,
                earlier=earlier,
                order=order,
                order_id=order_id,
            )  # fmt: skip
        )
        return True

    def _pending_done(self, key: tuple[Variant, str, str], task: asyncio.Task[Any]) -> None:
        group = self._pending.get(key)
        if group is not None:
            group.discard(task)
            if not group:
                self._pending.pop(key, None)

    # --- entries ------------------------------------------------------------------------------------------------------

    def enter(
        self,
        order: LeaderOrder,
        order_id: int,
        entry: WatchEntry,
        v: Variant,
        snap: BookSnapshot | None,
        info: MarketInfo,
        *,
        scheduled: float | None = None,
        seq: int | None = None,
    ) -> EntryOutcome:
        cfg, risk = self.cfg, self.risk[v]
        self._observe(snap)
        book = snap.book if snap is not None else None
        ts = snap.observed_at if snap is not None else self.clock()
        lot = Lot(
            period=PERIOD,
            strategy=v.strategy,
            wallet=order.wallet,
            user_name=entry.user_name or order.name,
            tier=entry.tier,
            style=entry.style,
            category=info.category,
            event_key=info.event_key or f"c:{order.condition_id}",
            condition_id=order.condition_id or info.condition_id,
            token_id=order.token_id,
            latency_s=v.delay_s,
            signal_ts=order.first_ts,
            leader_price=order.vwap,
            leader_worst=order.worst_price,
            leader_notional=order.notional,
            exec_ts=int(ts),
        )
        exposure = Exposure(lot.condition_id, lot.event_key, lot.wallet, lot.category)
        rate = taker_fee_rate(info.category, info.fee_rate, int(ts))
        cap = order.vwap + cfg.max_premium
        out = EntryOutcome(order, order_id, entry, v, lot, info, snap, cap=cap)
        diag: dict[str, Any] = {
            "cap": cap,
            "fee_rate": rate,
            "market_fee_rate": info.fee_rate,
            "info_complete": info.complete,
            "detected_at": order.detected_at,
            "leader_taker": order.is_taker,
        }
        fill: Fill | None = None
        if book is None:
            lot.status = "skip:no_book"
            diag["error"] = snap.error if snap is not None else "no snapshot"
        else:
            lot.ref_price = book.mid
            best_ask = book.best_ask
            stake = risk.desired_stake(best_ask if best_ask is not None else order.vwap, entry.theta_shrunk, rate)
            probe_stake = stake if stake > 0 else cfg.risk.stake_usdc
            out.stake, out.probe_stake = stake, probe_stake
            out.probe = probe = book.vwap_buy(probe_stake, max_price=cap)
            out.uncapped = uncapped = book.vwap_buy(probe_stake)
            diag |= _book_stats(snap, cfg.depth_within) | {
                "desired_stake": stake,
                "usdc_under_cap": book.usdc_up_to(cap),
                "probe_vwap": probe.avg_price,
                "probe_usdc": probe.usdc,
                "uncapped_vwap": uncapped.avg_price,
                "uncapped_worst": uncapped.worst_price,
                "uncapped_complete": uncapped.complete,
            }
            if stake <= 0:
                lot.status = "skip:no_edge"
            elif best_ask is None or best_ask > cap + EPS:
                lot.status = "skip:premium"
            elif probe.avg_price is None or probe.usdc + 1e-6 < stake * cfg.min_fill_ratio:
                lot.status = "skip:depth"
            elif not cfg.min_price <= probe.avg_price <= cfg.max_price:
                lot.status = "skip:extreme_price"
            elif probe.shares < book.min_order_size:
                lot.status = "skip:min_size"
            else:
                decision = out.risk = risk.check(probe.usdc, exposure, ts)
                if not decision.approved:
                    lot.status = f"skip:{decision.reason}"
                else:
                    fill = probe if decision.stake >= probe.usdc - 1e-9 else book.vwap_buy(decision.stake, cap)
                    if fill.shares < book.min_order_size:
                        lot.status, fill = "skip:min_size", None
        lot.bench_cost = fill.usdc if fill is not None else cfg.risk.stake_usdc
        ol = OpenLot(
            0,
            lot,
            v,
            exposure,
            info.fee_rate,
            seq=self.next_seq() if seq is None else seq,
            bench_shares=lot.bench_cost / order.vwap if order.vwap > 0 else 0.0,
        )
        if fill is not None:
            fee = sum(taker_fee(leg.size, leg.price, rate) for leg in fill.legs)
            lot.status = FILLED
            lot.exec_price, lot.shares, lot.cost, lot.entry_fee = fill.avg_price, fill.shares, fill.usdc, fee
            lot.flows.append((lot.exec_ts or int(ts), -fee, fill.usdc))
            ol.shares, ol.basis = fill.shares, fill.usdc
            risk.open(exposure, fill.usdc)
            risk.realize(-fee, ts)
        out.usage = risk.usage(exposure)
        diag["risk_usage"] = out.usage
        snapshot_id = None
        if snap is not None:
            row = self._snapshot_row(
                snap,
                purpose="entry",
                variant=v,
                order_id=order_id,
                scheduled=scheduled,
                leader_ts=order.first_ts,
                fill=out.probe,
            )
            snapshot_id = self.store.insert_snapshot(row)
        ol.lot_id = out.lot_id = self.store.insert_lot(
            lot,
            variant=v.name,
            order_id=order_id,
            snapshot_id=snapshot_id,
            open_shares=ol.shares,
            open_basis=ol.basis,
            bench_shares=ol.bench_shares,
            diag=diag,
        )
        self.positions[v].setdefault((lot.wallet, lot.token_id), []).append(ol)
        self.counters[f"{v.name}:{lot.status}"] += 1
        if lot.token_id in self.resolved:
            self.settle(lot.token_id, self.resolved[lot.token_id], ts)
        self._refresh_equity(v)
        return out

    # --- exits --------------------------------------------------------------------------------------------------------

    def exit(
        self,
        v: Variant,
        wallet: str,
        token_id: str,
        fraction: float,
        snap: BookSnapshot | None,
        *,
        bench_price: float,
        kind: str = "sell",
        before_seq: int | None = None,
        order_id: int | None = None,
        scheduled: float | None = None,
    ) -> ExitOutcome:
        """Mirror a leader reduction of `fraction` on the lots copied from `wallet` in `token_id` (those detected
        before `before_seq`): the benchmark at `bench_price`, the follower by selling into the snapshot's bids."""
        self._observe(snap)
        ts = int(snap.observed_at) if snap is not None else int(self.clock())
        frac = min(1.0, max(0.0, fraction))
        out = ExitOutcome(v, wallet, token_id, kind, frac, snapshot=snap)
        key = (wallet, token_id)
        lots = [ol for ol in self.positions[v].get(key, []) if before_seq is None or ol.seq < before_seq]
        touched: list[OpenLot] = []
        for ol in lots:
            if ol.bench_shares > EPS and frac > 0:
                q = ol.bench_shares * frac
                ol.lot.bench_value += q * bench_price
                ol.bench_shares -= q
                touched.append(ol)
        holders = [ol for ol in lots if ol.shares > EPS]
        snapshot_id = None
        if holders and frac > 0:
            want = sum(ol.shares for ol in holders) * frac
            book = snap.book if snap is not None else None
            fill = book.vwap_sell(want) if book is not None else None
            if snap is not None:
                row = self._snapshot_row(
                    snap, purpose=kind, variant=v, order_id=order_id, scheduled=scheduled, fill=fill
                )
                snapshot_id = self.store.insert_snapshot(row)
            if fill is None:
                out.status = "no_book"
            elif fill.shares <= EPS:
                out.status = "no_bids"
            else:
                out.status = "ok" if fill.complete else "partial"
                rate = taker_fee_rate(holders[0].lot.category, holders[0].fee_rate, ts)
                total_fee = sum(taker_fee(leg.size, leg.price, rate) for leg in fill.legs)
                sold = fill.shares / want
                for ol in holders:
                    q = ol.shares * frac * sold
                    part = q / fill.shares
                    proceeds, fee = fill.usdc * part, total_fee * part
                    basis = ol.basis * q / ol.shares
                    pnl = proceeds - fee - basis
                    lot = ol.lot
                    lot.exit_proceeds += proceeds
                    lot.exit_fee += fee
                    lot.flows.append((ts, pnl, -basis))
                    ol.shares -= q
                    ol.basis -= basis
                    if ol.shares <= EPS * max(1.0, lot.shares):
                        ol.shares = ol.basis = 0.0
                        lot.close_ts = ts
                    self.risk[v].release(ol.exposure, basis)
                    self.risk[v].realize(pnl, ts)
                    self.store.insert_exit(
                        {
                            "lot_id": ol.lot_id,
                            "order_id": order_id,
                            "snapshot_id": snapshot_id,
                            "variant": v.name,
                            "kind": kind,
                            "ts": ts,
                            "fraction": frac,
                            "shares": q,
                            "price": proceeds / q if q > 0 else None,
                            "proceeds": proceeds,
                            "fee": fee,
                            "basis": basis,
                            "pnl": pnl,
                            "status": out.status,
                        }
                    )
                    out.shares += q
                    out.proceeds += proceeds
                    out.fee += fee
                    out.basis += basis
                    out.lots += 1
                    if ol not in touched:
                        touched.append(ol)
            if out.status in ("no_book", "no_bids"):
                for ol in holders:
                    self.store.insert_exit(
                        {
                            "lot_id": ol.lot_id,
                            "order_id": order_id,
                            "snapshot_id": snapshot_id,
                            "variant": v.name,
                            "kind": kind,
                            "ts": ts,
                            "fraction": frac,
                            "shares": 0.0,
                            "status": out.status,
                        }
                    )
        for ol in touched:
            self._save(ol)
        self._prune(v, key)
        self._refresh_equity(v)
        self.counters[f"{v.name}:exit_{out.status}"] += 1
        return out

    def settle(self, token_id: str, final_price: float, ts: float | None = None) -> list[Settled]:
        """Settle every open follower and benchmark share of a resolved token at `final_price`."""
        ts_i = int(ts if ts is not None else self.clock())
        if token_id not in self.resolved:
            cid = next((ol.lot.condition_id for v in self.variants for ol in self.lots_of(v)
                        if ol.lot.token_id == token_id), "")  # fmt: skip
            self.store.upsert_settlement(token_id, cid, final_price, ts_i)
        self.resolved[token_id] = final_price
        closed: list[Settled] = []
        for v in self.variants:
            for key in [k for k in self.positions[v] if k[1] == token_id]:
                for ol in self.positions[v][key]:
                    changed = False
                    if ol.bench_shares > EPS:
                        ol.lot.bench_value += ol.bench_shares * final_price
                        ol.bench_shares = 0.0
                        changed = True
                    if ol.shares > EPS:
                        shares, basis = ol.shares, ol.basis
                        value = shares * final_price
                        lot = ol.lot
                        lot.settle_value += value
                        lot.flows.append((ts_i, value - basis, -basis))
                        lot.close_ts = ts_i
                        self.risk[v].release(ol.exposure, basis)
                        self.risk[v].realize(value - basis, ts_i)
                        self.store.insert_exit(
                            {
                                "lot_id": ol.lot_id,
                                "variant": v.name,
                                "kind": "settle",
                                "ts": ts_i,
                                "fraction": 1.0,
                                "shares": shares,
                                "price": final_price,
                                "proceeds": value,
                                "fee": 0.0,
                                "basis": basis,
                                "pnl": value - basis,
                                "status": "ok",
                            }
                        )
                        ol.shares = ol.basis = 0.0
                        closed.append(Settled(v, ol, shares, value, basis))
                        changed = True
                    if changed:
                        self._save(ol)
                self._prune(v, key)
            self._refresh_equity(v)
        return closed

    def mark(self, mids: dict[str, float], ts: float | None = None) -> list[dict[str, Any]]:
        """Record mark-to-market equity per variant (open positions at mid; unmarked ones at cost)."""
        self.marks.update(mids)
        ts_i = int(ts if ts is not None else self.clock())
        rows = []
        for v in self.variants:
            unreal, n_open, unmarked = self.unrealized(v)
            self._refresh_equity(v)
            risk = self.risk[v]
            row = {
                "ts": ts_i,
                "variant": v.name,
                "bankroll": self.cfg.risk.bankroll,
                "realized": risk.realized,
                "unrealized": unreal,
                "equity": self.cfg.risk.bankroll + risk.realized + unreal,
                "deployed": risk.total_deployed,
                "open_lots": n_open,
                "unmarked": unmarked,
                "killed": int(risk.killed),
            }
            self.store.insert_equity(row)
            rows.append(row)
        return rows

    def reconcile(
        self, wallet: str, api_positions: dict[str, float], now: float | None = None
    ) -> list[tuple[str, float]]:
        """Sync a leader's holdings with /positions for tokens quiet on the feed.

        Returns (token, fraction) reductions on tokens we copied: merges, transfers or missed sells to mirror.
        """
        now = self.clock() if now is None else now
        held = self.holdings[wallet]
        out: list[tuple[str, float]] = []
        for token_id in set(held) | set(api_positions):
            if now - self.last_activity.get((wallet, token_id), -math.inf) < self.cfg.reconcile_quiet_s:
                continue
            local, remote = held.get(token_id, 0.0), api_positions.get(token_id, 0.0)
            if (
                local > EPS
                and remote < local * (1 - self.cfg.reconcile_tolerance)
                and token_id not in self.resolved
                and self.has_any_position(wallet, token_id)
            ):
                out.append((token_id, (local - remote) / local))
            if remote > EPS:
                held[token_id] = remote
            else:
                held.pop(token_id, None)
        return out

    def set_holdings(self, wallet: str, positions: dict[str, float]) -> None:
        self.holdings[wallet] = {t: s for t, s in positions.items() if s > EPS}

    def pause(self) -> None:
        for v in self.variants:
            self.risk[v].paused = True
            self.store.set_meta(f"risk:{v.name}", json.dumps(self.risk[v].state()))

    def resume(self) -> None:
        for v in self.variants:
            self.risk[v].resume()
            self.store.set_meta(f"risk:{v.name}", json.dumps(self.risk[v].state()))

    @property
    def paused(self) -> bool:
        return all(self.risk[v].paused for v in self.variants)

    # --- restart ------------------------------------------------------------------------------------------------------

    def load(self) -> int:
        """Rebuild open positions, realized PnL history and risk state of this strategy from the store."""
        by_name = {v.name: v for v in self.variants}
        n_open = 0
        for row in self.store.lot_rows(self.cfg.strategy):
            v = by_name.get(row["variant"])
            if v is None:
                continue
            lot = lot_from_json(row["lot_json"])
            risk = self.risk[v]
            for ts, dp, _ in lot.flows:
                risk.realize(dp, ts)
            if row["open_shares"] <= EPS and row["bench_shares"] <= EPS:
                continue
            diag = json.loads(row["diag_json"] or "{}")
            ol = OpenLot(
                row["id"],
                lot,
                v,
                Exposure(lot.condition_id, lot.event_key, lot.wallet, lot.category),
                float(diag.get("market_fee_rate") or 0.0),
                shares=row["open_shares"],
                basis=row["open_basis"],
                bench_shares=row["bench_shares"],
            )
            self.positions[v].setdefault((lot.wallet, lot.token_id), []).append(ol)
            if ol.shares > EPS:
                risk.open(ol.exposure, ol.basis)
            n_open += 1
        for title_row in self.store.rows("SELECT token_id, title, outcome FROM orders WHERE title != ''"):
            self.titles[title_row["token_id"]] = f"{title_row['title']} · {title_row['outcome']}"
        self.resolved.update(self.store.settlements())
        for v in self.variants:
            state = self.store.get_meta(f"risk:{v.name}")
            if state:
                self.risk[v].restore(json.loads(state))
            self._refresh_equity(v)
        return n_open

    # --- async orchestration ------------------------------------------------------------------------------------------

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.counters["task_errors"] += 1
            log.error("paper task failed", exc_info=task.exception())

    @property
    def pending_tasks(self) -> int:
        return len(self._tasks)

    async def _sleep_until(self, due: float) -> None:
        delay = due - self.clock()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _notify_trips(self) -> None:
        while self._tripped:
            v = self._tripped.pop(0)
            await self.listener.kill_switch(v, self.risk[v].equity, self.risk[v].peak_equity)

    async def _info(self, task: asyncio.Task[MarketInfo], order: LeaderOrder) -> MarketInfo:
        try:
            return await asyncio.wait_for(asyncio.shield(task), self.cfg.info_timeout_s)
        except Exception:  # timeout or lookup failure: fall back, never drop the signal
            return MarketInfo.fallback(order.token_id, order.condition_id)

    async def _entry_task(
        self,
        order: LeaderOrder,
        order_id: int,
        entry: WatchEntry,
        v: Variant,
        info_task: asyncio.Task[MarketInfo],
        seq: int,
    ) -> None:
        due = order.detected_at + v.delay_s
        await self._sleep_until(due)
        snap = await self.books.fetch(order.token_id)
        info = await self._info(info_task, order)
        outcome = self.enter(order, order_id, entry, v, snap, info, scheduled=due, seq=seq)
        await self.listener.entry(outcome)
        await self._notify_trips()

    async def _exit_task(
        self,
        v: Variant,
        wallet: str,
        token_id: str,
        fraction: float,
        *,
        kind: str,
        due: float,
        bench_price: float,
        seq: int,
        earlier: list[asyncio.Task[Any]],
        order: LeaderOrder | None = None,
        order_id: int | None = None,
    ) -> None:
        await self._sleep_until(due)
        if earlier:
            await asyncio.gather(*earlier, return_exceptions=True)
        holders = any(ol.shares > EPS for ol in self.positions[v].get((wallet, token_id), []))
        snap = await self.books.fetch(token_id) if holders else None
        outcome = self.exit(
            v, wallet, token_id, fraction, snap, bench_price=bench_price, kind=kind, before_seq=seq,
            order_id=order_id, scheduled=due,
        )  # fmt: skip
        await self.listener.exit(outcome, order)
        await self._notify_trips()

    async def apply_reductions(self, wallet: str, reductions: list[tuple[str, float]], now: float | None = None) -> int:
        """Mirror /positions reductions: settle tokens that resolved, schedule merge exits for the rest."""
        if not reductions:
            return 0
        now = self.clock() if now is None else now
        try:
            resolved = await self.markets.resolutions([t for t, _ in reductions])
        except Exception as exc:
            log.warning("resolution lookup failed: %s", exc)
            resolved = {}
        scheduled = 0
        for token_id, frac in reductions:
            if token_id in resolved:
                closed = self.settle(token_id, resolved[token_id])
                await self.listener.settled(token_id, resolved[token_id], closed)
                continue
            snap = await self.books.fetch(token_id)
            self._observe(snap)
            book = snap.book
            price = book.mid if book is not None and book.mid is not None else self.marks.get(token_id)
            if price is None:
                price = book.best_bid if book is not None and book.best_bid is not None else 0.0
            self.counters["merge_exits"] += 1
            seq = self.next_seq()
            for v in self.variants:
                scheduled += self._schedule_exit(
                    v, wallet, token_id, frac, kind="merge", due=now + v.delay_s, bench_price=price, seq=seq
                )
        await self._notify_trips()
        return scheduled

    async def check_resolutions(self) -> int:
        tokens = self.open_tokens()
        if not tokens:
            return 0
        resolved = await self.markets.resolutions(tokens)
        for token_id, price in resolved.items():
            closed = self.settle(token_id, price)
            await self.listener.settled(token_id, price, closed)
        await self._notify_trips()
        return len(resolved)

    async def mark_to_market(self, *, concurrency: int = 8) -> list[dict[str, Any]]:
        tokens = sorted(self.held_tokens())
        sem = asyncio.Semaphore(concurrency)

        async def one(token_id: str) -> BookSnapshot:
            async with sem:
                return await self.books.fetch(token_id)

        snaps = await asyncio.gather(*(one(t) for t in tokens))
        mids: dict[str, float] = {}
        for snap in snaps:
            self.store.insert_snapshot(self._snapshot_row(snap, purpose="mtm", levels=False))
            if snap.book is not None and snap.book.mid is not None:
                mids[snap.token_id] = snap.book.mid
        rows = self.mark(mids)
        await self._notify_trips()
        return rows

    async def drain(self, timeout: float) -> int:
        """Wait for scheduled snapshots to complete; returns how many were still pending at the timeout."""
        deadline = self.clock() + timeout
        while self._tasks:
            left = deadline - self.clock()
            if left <= 0:
                break
            await asyncio.wait(set(self._tasks), timeout=left)
        return len(self._tasks)

    async def cancel_all(self) -> int:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)
