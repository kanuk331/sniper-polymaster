"""Copy-trading backtest: one `metrics.Lot` per leader order, with the follower's execution and cash flows.

The follower reacts to each leader order after `latency_s`, pays the first CLOB history price at or after that time
plus `half_spread`, pays the taker fee in force at that time (`fees.taker_fee_rate`), and skips signals whose price
ran away (`max_premium` above the leader's VWAP) or sits outside [min_price, max_price]. The history is ~1-minute
bars of last/mid prices, not an order book: depth and impact are not modelled, and the effective delay is
quantised to the next bar (`Lot.exec_ts` records the bar used). The follower never fills better than the leader:
after the leader sweeps the book, the cheap levels are gone, so the entry is at least the leader's worst level and
exits are at most the leader's worst sell level.

Exits mirror the leader's sells/merges proportionally; everything else is held to resolution (the market's close
time is used as the settlement time). Positions unresolved at evaluation time are marked at cost (zero PnL).

The leader benchmark replays every signal (filled or not) with the same stake at the leader's VWAP, without fees,
following the leader's exits; for skipped signals it measures the PnL the follower missed.
"""

from __future__ import annotations

import bisect
import logging
import random
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sniper.analytics import Meta, Row
from sniper.fees import taker_fee, taker_fee_rate
from sniper.metrics import Lot
from sniper.scoring import WalletScore

log = logging.getLogger(__name__)

PriceFn = Callable[[str, int], float | None]


@dataclass(frozen=True)
class CopyParams:
    latency_s: int = 60
    stake_usdc: float = 100.0
    half_spread: float = 0.01
    max_premium: float = 0.015  # skip if follower price > leader VWAP + max_premium
    min_price: float = 0.03
    max_price: float = 0.97
    max_signals: int = 300  # per wallet and window, earliest first
    mirror_exits: bool = True
    strategy: str = "baseline"
    # "order": every leader BUY order is a signal (phase-1 baseline).
    # "position": one signal per leader position, when its open cost first reaches min_leader_notional. Fills of one
    # resting maker order arrive as many tiny "orders" in separate transactions; copying each with a fixed stake
    # multiplies exposure far beyond the leader's own (item 1 of research iteration 2).
    signal_mode: str = "order"
    min_leader_notional: float = 0.0


BASELINE = CopyParams(strategy="baseline")
POSITION = CopyParams(strategy="position", signal_mode="position", min_leader_notional=100.0)
STRATEGIES = {"baseline": BASELINE, "position": POSITION}


@dataclass(frozen=True)
class LotLabels:
    period: str = ""
    user_name: str = ""
    tier: str = ""
    style: str = ""


class PriceBook:
    """First cached CLOB price point at or after a timestamp, within `max_wait_s`."""

    def __init__(self, series: dict[str, list[tuple[int, float]]], max_wait_s: int = 600):
        self.series = {k: sorted(v) for k, v in series.items()}
        self.stamps = {k: [t for t, _ in v] for k, v in self.series.items()}
        self.max_wait_s = max_wait_s

    def point(self, token_id: str, ts: int) -> tuple[int, float] | None:
        stamps = self.stamps.get(token_id)
        if not stamps:
            return None
        i = bisect.bisect_left(stamps, ts)
        if i == len(stamps) or stamps[i] - ts > self.max_wait_s:
            return None
        return self.series[token_id][i]

    def __call__(self, token_id: str, ts: int) -> float | None:
        p = self.point(token_id, ts)
        return None if p is None else p[1]


def aggregate_orders(rows: Sequence[Row]) -> list[dict[str, Any]]:
    """Collapse the fills of one leader order (same tx, token and side) into a single signal.

    A taker order that sweeps the book shows up as many fills at rising prices. Copying each fill would hand the
    follower the cheap levels the leader already consumed, so a signal carries the VWAP (`price`) and the worst
    level reached (`worst_price`: highest for buys, lowest for sells), which bounds what the follower can get.
    """
    out: list[dict[str, Any]] = []
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        if r["type"] != "TRADE" or not r["token_id"] or not r["tx_hash"]:
            out.append(dict(r))
            continue
        key = (r["tx_hash"], r["token_id"], r["side"])
        agg = index.get(key)
        if agg is None:
            agg = dict(r) | {"notional": r["size"] * r["price"], "worst_price": r["price"]}
            index[key] = agg
            out.append(agg)
            continue
        agg["size"] += r["size"]
        agg["notional"] += r["size"] * r["price"]
        agg["price"] = agg["notional"] / agg["size"] if agg["size"] else r["price"]
        worse = max if r["side"] == "BUY" else min
        agg["worst_price"] = worse(agg["worst_price"], r["price"])
    return out


@dataclass
class _LeaderPosition:
    cost: float = 0.0
    shares: float = 0.0
    signaled: bool = False


@dataclass
class _Position:
    lot: Lot
    shares: float = 0.0  # follower shares still held
    basis: float = 0.0  # follower cost basis still held
    bench_shares: float = 0.0


def simulate_copy(
    rows: Sequence[Row],
    meta: Meta,
    book: PriceBook,
    *,
    start: int,
    end: int,
    params: CopyParams,
    wallet: str = "",
    labels: LotLabels | None = None,
    resolve_as_of: int | None = None,
) -> list[Lot]:
    """Replay a leader's activity (ts-ordered, may include history before `start`) and copy BUY orders in [start, end).

    `resolve_as_of` limits which resolutions are known (None = everything known now); in-sample copyability tests
    pass the scoring cutoff so they never see later outcomes.
    """
    labels = labels or LotLabels()
    lots: list[Lot] = []
    leader: dict[str, float] = defaultdict(float)  # leader's real holdings, needed to size proportional exits
    positions: dict[str, list[_Position]] = defaultdict(list)
    leader_pos: dict[str, _LeaderPosition] = defaultdict(_LeaderPosition)
    n_signals = 0

    def signal_for(r: Row, tid: str) -> Row | None:
        """Apply the signal definition to a leader BUY order; returns the signal row or None."""
        if params.signal_mode == "order":
            return r if r["size"] * r["price"] >= params.min_leader_notional else None
        lp = leader_pos[tid]
        lp.cost += r["size"] * r["price"]
        lp.shares += r["size"]
        if lp.signaled or lp.cost < params.min_leader_notional:
            return None
        lp.signaled = True
        # Premium is measured against the leader's average entry; liquidity against the triggering order.
        return dict(r) | {"price": lp.cost / lp.shares, "size": lp.shares}

    def reduce_leader(tid: str, frac: float) -> None:
        lp = leader_pos[tid]
        lp.cost *= 1 - frac
        lp.shares *= 1 - frac
        if leader[tid] <= 1e-6:
            leader_pos[tid] = _LeaderPosition()

    def fee_rate(token_id: str, ts: int) -> float:
        market = meta.markets.get(meta.tokens[token_id].condition_id)
        return taker_fee_rate(market.category, market.fee_rate, ts) if market else 0.0

    def buy(r: Row, token_id: str) -> None:
        token = meta.tokens[token_id]
        market = meta.markets.get(token.condition_id)
        stake = params.stake_usdc
        lot = Lot(
            period=labels.period,
            strategy=params.strategy,
            wallet=wallet,
            user_name=labels.user_name,
            tier=labels.tier,
            style=labels.style,
            category=market.category if market else "unknown",
            event_key=token.event_key,
            condition_id=token.condition_id,
            token_id=token_id,
            latency_s=params.latency_s,
            signal_ts=r["ts"],
            leader_price=r["price"],
            leader_worst=r.get("worst_price", r["price"]),
            leader_notional=r["size"] * r["price"],
            bench_cost=stake,
        )
        lots.append(lot)
        pos = _Position(lot, bench_shares=stake / r["price"] if r["price"] > 0 else 0.0)
        positions[token_id].append(pos)
        point = book.point(token_id, r["ts"] + params.latency_s)
        if point is None:
            lot.status = "skip:no_price"
            return
        lot.exec_ts, lot.ref_price = point
        price = max(lot.ref_price + params.half_spread, lot.leader_worst)
        if price > r["price"] + params.max_premium:
            lot.status = "skip:premium"
            return
        if not params.min_price <= price <= params.max_price:
            lot.status = "skip:extreme_price"
            return
        shares = stake / price
        fee = taker_fee(shares, price, fee_rate(token_id, lot.exec_ts))
        lot.exec_price, lot.shares, lot.cost, lot.entry_fee = price, shares, stake, fee
        lot.flows.append((lot.exec_ts, -fee, stake))
        pos.shares, pos.basis = shares, stake

    def exit_(token_id: str, frac: float, ts: int, vwap: float, worst: float, *, leader_traded: bool) -> None:
        held = positions.get(token_id) or []
        if frac <= 0 or not held:
            return
        for pos in held:
            if pos.bench_shares > 0:
                q = pos.bench_shares * frac
                pos.lot.bench_value += q * vwap
                pos.bench_shares -= q
        followers = [p for p in held if p.shares > 1e-12]
        if not params.mirror_exits or not followers:
            return
        point = book.point(token_id, ts + params.latency_s)
        exit_ts, ref = point if point else (ts + params.latency_s, worst)
        price = max(0.0, ref - params.half_spread)
        if leader_traded:
            price = min(price, worst)  # no better than the leader's own sale
        rate = fee_rate(token_id, exit_ts)
        for pos in followers:
            q, basis = pos.shares * frac, pos.basis * frac
            proceeds, fee = q * price, taker_fee(q, price, rate)
            pos.lot.exit_proceeds += proceeds
            pos.lot.exit_fee += fee
            pos.lot.flows.append((exit_ts, proceeds - basis - fee, -basis))
            pos.shares -= q
            pos.basis -= basis
            if pos.shares <= 1e-9:
                pos.lot.close_ts = exit_ts

    for r in aggregate_orders(rows):
        token = meta.tokens.get(r["token_id"]) if r["token_id"] else None
        in_window = start <= r["ts"] < end
        kind = r["type"]
        if kind == "TRADE" and token is not None:
            tid = token.token_id
            before = leader[tid]
            if r["side"] == "BUY":
                leader[tid] += r["size"]
                sig = signal_for(r, tid)
                if sig is not None and in_window and n_signals < params.max_signals:
                    n_signals += 1
                    buy(sig, tid)
            else:
                leader[tid] = max(0.0, before - r["size"])
                if before > 0:
                    frac = min(1.0, r["size"] / before)
                    reduce_leader(tid, frac)
                    exit_(tid, frac, r["ts"], r["price"], r.get("worst_price", r["price"]), leader_traded=True)
        elif kind in ("MERGE", "SPLIT"):
            for t in (meta.by_condition.get(r["condition_id"]) or {}).values():
                before = leader[t.token_id]
                if kind == "SPLIT":
                    leader[t.token_id] += r["size"]
                    continue
                leader[t.token_id] = max(0.0, before - r["size"])
                if before > 0:
                    frac = min(1.0, r["size"] / before)
                    reduce_leader(t.token_id, frac)
                    # A merge redeems pairs at $1; value each leg at the current price.
                    p = book(t.token_id, r["ts"]) or 0.5
                    exit_(t.token_id, frac, r["ts"], p, p, leader_traded=False)

    for token_id, held in positions.items():
        token = meta.tokens[token_id]
        y0 = meta.resolution(token.condition_id, resolve_as_of)
        market = meta.markets.get(token.condition_id)
        for pos in held:
            lot = pos.lot
            if y0 is None:
                lot.open_cost = pos.basis
                lot.bench_value += pos.bench_shares * lot.leader_price  # marked at cost, like the follower
                continue
            payout = y0 if token.outcome_index == 0 else 1.0 - y0
            lot.bench_value += pos.bench_shares * payout
            if pos.shares > 1e-12:
                settle_ts = max(market.closed_time or end, lot.exec_ts or end) if market else end
                value = pos.shares * payout
                lot.settle_value += value
                lot.flows.append((settle_ts, value - pos.basis, -pos.basis))
                lot.close_ts = settle_ts
    return lots


# --- price windows and wallet selection --------------------------------------------------------------------------


def signal_points(rows: Sequence[Row], start: int, end: int, meta: Meta | None = None) -> list[tuple[str, int]]:
    """(token, ts) pairs whose prices a simulation of [start, end) may need: trades, plus merged legs."""
    points = []
    for r in rows:
        if not start <= r["ts"] < end:
            continue
        if r["type"] == "TRADE" and r["token_id"]:
            points.append((r["token_id"], r["ts"]))
        elif r["type"] == "MERGE" and meta is not None:
            points += [(t.token_id, r["ts"]) for t in (meta.by_condition.get(r["condition_id"]) or {}).values()]
    return points


def choose_wallets_to_simulate(
    scores: Sequence[WalletScore],
    *,
    n_noise: int = 100,
    n_insufficient: int = 60,
    n_per_exclusion: int = 40,
    seed: int = 0,
) -> list[WalletScore]:
    """All candidate/watch wallets plus seeded samples of the other tiers as comparison groups.

    Excluded wallets are sampled per primary exclusion reason (`style`), so every filter is checked out of sample.
    """
    rng = random.Random(seed)
    by_group: dict[str, list[WalletScore]] = defaultdict(list)
    for s in scores:
        key = f"excluded:{s.style}" if s.tier == "excluded" else s.tier
        by_group[key].append(s)
    chosen = by_group.pop("candidate", []) + by_group.pop("watch", [])
    for key in sorted(by_group):
        n = n_noise if key == "noise" else n_insufficient if key == "insufficient" else n_per_exclusion
        pool = sorted(by_group[key], key=lambda s: s.wallet)
        chosen += rng.sample(pool, min(n, len(pool)))
    return chosen


def run_simulations(
    db: Any,
    meta: Meta,
    scores: Sequence[WalletScore],
    *,
    start: int,
    end: int,
    latencies: Sequence[int],
    base: CopyParams,
    book: PriceBook,
    period: str = "",
    strategies: Sequence[CopyParams] | None = None,
) -> list[Lot]:
    lots: list[Lot] = []
    for s in scores:
        rows = db.activity(s.wallet, until=end)
        labels = LotLabels(period=period, user_name=s.user_name or "", tier=s.tier, style=s.style)
        for strategy in strategies or [base]:
            for latency in latencies:
                params = CopyParams(**(strategy.__dict__ | {"latency_s": latency}))
                lots += simulate_copy(
                    rows, meta, book, start=start, end=end, params=params, wallet=s.wallet, labels=labels
                )
    return lots


# --- predictive check ---------------------------------------------------------------------------------------------


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 5:
        return None

    def ranks(v: Sequence[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def predictive_check(
    lots: Sequence[Lot], scores: Sequence[WalletScore], latency_s: int, min_fills: int = 5
) -> dict[str, Any]:
    """Does the pre-split edge estimate rank wallets by out-of-sample follower ROI?"""
    theta = {s.wallet: s.theta_shrunk for s in scores}
    pnl: dict[str, float] = defaultdict(float)
    cost: dict[str, float] = defaultdict(float)
    fills: dict[str, int] = defaultdict(int)
    for lot in lots:
        if lot.latency_s == latency_s and lot.filled:
            pnl[lot.wallet] += lot.pnl
            cost[lot.wallet] += lot.cost
            fills[lot.wallet] += 1
    pairs = [(theta[w], pnl[w] / cost[w]) for w in fills if fills[w] >= min_fills and w in theta]
    return {
        "latency_s": latency_s,
        "wallets": len(pairs),
        "spearman_theta_vs_follower_roi": spearman([a for a, _ in pairs], [b for _, b in pairs]),
    }
