"""Copy-trading backtest and walk-forward evaluation.

The follower reacts to each leader fill after `latency_s`, pays the CLOB history price plus `half_spread` (the
history is ~1-minute last/mid prices, not an order book, so depth and market impact are not modelled), pays the
category taker fee, and skips signals whose price ran away. Exits mirror the leader's sells/merges proportionally;
everything else is held to resolution. Positions still unresolved are marked at cost (zero PnL).

The leader benchmark replays the same signals with the same stake at the leader's own prices and without fees, so
`copy_efficiency` isolates what latency, spread, fees and skipped signals cost the follower.
"""

from __future__ import annotations

import bisect
import logging
import random
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from sniper.analytics import Meta, Row, load_meta
from sniper.scoring import TIERS, Thresholds, WalletScore, score_wallets

log = logging.getLogger(__name__)

PriceFn = Callable[[str, int], float | None]


@dataclass(frozen=True)
class CopyParams:
    latency_s: int = 60
    stake_usdc: float = 100.0
    half_spread: float = 0.01
    max_premium: float = 0.015  # skip if follower price > leader price + max_premium
    min_price: float = 0.03
    max_price: float = 0.97
    max_signals: int = 300  # per wallet, earliest first
    mirror_exits: bool = True


@dataclass
class CopyResult:
    wallet: str
    latency_s: int
    n_signals: int = 0
    n_filled: int = 0
    skips: Counter = field(default_factory=Counter)
    cost: float = 0.0
    fees: float = 0.0
    proceeds: float = 0.0
    settled: float = 0.0
    open_cost: float = 0.0
    premium_sum: float = 0.0
    leader_cost: float = 0.0
    leader_value: float = 0.0

    @property
    def pnl(self) -> float:
        return self.proceeds + self.settled + self.open_cost - self.cost - self.fees

    @property
    def roi(self) -> float:
        return self.pnl / self.cost if self.cost else 0.0

    @property
    def leader_pnl(self) -> float:
        return self.leader_value - self.leader_cost

    @property
    def leader_roi(self) -> float:
        return self.leader_pnl / self.leader_cost if self.leader_cost else 0.0

    @property
    def mean_premium(self) -> float:
        return self.premium_sum / self.n_filled if self.n_filled else 0.0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["skips"] = dict(self.skips)
        d |= {"pnl": self.pnl, "roi": self.roi, "leader_pnl": self.leader_pnl, "leader_roi": self.leader_roi,
              "mean_premium": self.mean_premium}  # fmt: skip
        return d


class PriceBook:
    """First cached CLOB price at or after a timestamp, within `max_wait_s`."""

    def __init__(self, series: dict[str, list[tuple[int, float]]], max_wait_s: int = 600):
        self.series = {k: sorted(v) for k, v in series.items()}
        self.stamps = {k: [t for t, _ in v] for k, v in self.series.items()}
        self.max_wait_s = max_wait_s

    def __call__(self, token_id: str, ts: int) -> float | None:
        stamps = self.stamps.get(token_id)
        if not stamps:
            return None
        i = bisect.bisect_left(stamps, ts)
        if i == len(stamps) or stamps[i] - ts > self.max_wait_s:
            return None
        return self.series[token_id][i][1]


def taker_fee(shares: float, price: float, rate: float) -> float:
    # docs.polymarket.com/trading/fees: fee = C * feeRate * p * (1 - p)
    return shares * rate * price * (1 - price)


@dataclass
class _Book:
    shares: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    cost: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    def reduce(self, token: str, frac: float) -> float:
        q = self.shares[token] * frac
        self.shares[token] -= q
        self.cost[token] -= self.cost[token] * frac
        return q


def simulate_copy(
    rows: Sequence[Row], meta: Meta, price_at: PriceFn, *, start: int, end: int, params: CopyParams, wallet: str = ""
) -> CopyResult:
    """Replay a leader's activity (full history, ts-ordered) and copy signals in [start, end)."""
    res = CopyResult(wallet=wallet, latency_s=params.latency_s)
    leader: dict[str, float] = defaultdict(float)  # leader's real holdings, needed to size proportional exits
    follower, bench = _Book(), _Book()

    def fee_rate(token: str) -> float:
        market = meta.markets.get(meta.tokens[token].condition_id)
        return market.fee_rate if market else 0.0

    def buy(r: Row, token: str) -> None:
        res.n_signals += 1
        p = price_at(token, r["ts"] + params.latency_s)
        if p is None:
            res.skips["no_price"] += 1
            return
        price = p + params.half_spread
        if price > r["price"] + params.max_premium:
            res.skips["premium"] += 1
            return
        if not params.min_price <= price <= params.max_price:
            res.skips["extreme_price"] += 1
            return
        shares = params.stake_usdc / price
        res.n_filled += 1
        res.cost += params.stake_usdc
        res.fees += taker_fee(shares, price, fee_rate(token))
        res.premium_sum += price - r["price"]
        follower.shares[token] += shares
        follower.cost[token] += params.stake_usdc
        bench.shares[token] += params.stake_usdc / r["price"]
        bench.cost[token] += params.stake_usdc
        res.leader_cost += params.stake_usdc

    def exit_(token: str, frac: float, ts: int, leader_price: float) -> None:
        if frac <= 0:
            return
        if bench.shares[token] > 0:
            res.leader_value += bench.reduce(token, frac) * leader_price
        if params.mirror_exits and follower.shares[token] > 0:
            p = price_at(token, ts + params.latency_s)
            p = max(0.0, (p if p is not None else leader_price) - params.half_spread)
            q = follower.reduce(token, frac)
            res.proceeds += q * p
            res.fees += taker_fee(q, p, fee_rate(token))

    for r in rows:
        token = meta.tokens.get(r["token_id"]) if r["token_id"] else None
        in_window = start <= r["ts"] < end
        kind = r["type"]
        if kind == "TRADE" and token is not None:
            tid = token.token_id
            before = leader[tid]
            if r["side"] == "BUY":
                leader[tid] += r["size"]
                if in_window and res.n_signals < params.max_signals:
                    buy(r, tid)
            else:
                leader[tid] = max(0.0, before - r["size"])
                if in_window and before > 0:
                    exit_(tid, min(1.0, r["size"] / before), r["ts"], r["price"])
        elif kind in ("MERGE", "SPLIT"):
            for t in (meta.by_condition.get(r["condition_id"]) or {}).values():
                before = leader[t.token_id]
                if kind == "SPLIT":
                    leader[t.token_id] += r["size"]
                    continue
                leader[t.token_id] = max(0.0, before - r["size"])
                if in_window and before > 0:
                    # A merge redeems pairs at $1; value each leg at the current price.
                    exit_(t.token_id, min(1.0, r["size"] / before), r["ts"], price_at(t.token_id, r["ts"]) or 0.5)

    for token_id in set(follower.shares) | set(bench.shares):
        t = meta.tokens[token_id]
        y0 = meta.resolution(t.condition_id, None)
        if y0 is None:
            res.open_cost += follower.cost[token_id]
            res.leader_value += bench.cost[token_id]
            continue
        payout = y0 if t.outcome_index == 0 else 1.0 - y0
        res.settled += follower.shares[token_id] * payout
        res.leader_value += bench.shares[token_id] * payout
    return res


# --- walk-forward -------------------------------------------------------------------------------------------------


def signal_points(rows: Sequence[Row], start: int, end: int) -> list[tuple[str, int]]:
    return [(r["token_id"], r["ts"]) for r in rows if r["type"] == "TRADE" and r["token_id"] and start <= r["ts"] < end]


def choose_wallets_to_simulate(
    scores: Sequence[WalletScore], *, include_excluded: int, include_noise: int, seed: int = 0
) -> list[WalletScore]:
    """All candidate/watch wallets plus seeded random samples of noise/excluded wallets as comparison groups."""
    rng = random.Random(seed)
    by_tier: dict[str, list[WalletScore]] = defaultdict(list)
    for s in scores:
        by_tier[s.tier].append(s)
    chosen = by_tier["candidate"] + by_tier["watch"]
    for tier, n in (("noise", include_noise), ("excluded", include_excluded), ("insufficient", include_noise)):
        pool = by_tier[tier]
        chosen += rng.sample(pool, min(n, len(pool)))
    return chosen


@dataclass
class TierSummary:
    tier: str
    latency_s: int
    wallets: int
    wallets_with_fills: int
    signals: int
    filled: int
    cost: float
    pnl: float
    roi: float
    leader_roi: float
    copy_efficiency: float | None  # follower pnl / leader pnl on the same signals
    median_wallet_roi: float | None
    share_wallets_profitable: float | None
    mean_premium: float
    skips: dict[str, int]


def summarize(results: Sequence[tuple[WalletScore, CopyResult]], latency_s: int) -> list[TierSummary]:
    out = []
    for tier in TIERS:
        group = [(s, r) for s, r in results if s.tier == tier and r.latency_s == latency_s]
        if not group:
            continue
        rs = [r for _, r in group]
        filled = [r for r in rs if r.n_filled > 0]
        cost = sum(r.cost for r in rs)
        pnl = sum(r.pnl for r in rs)
        leader_pnl = sum(r.leader_pnl for r in rs)
        leader_cost = sum(r.leader_cost for r in rs)
        skips: Counter = Counter()
        for r in rs:
            skips.update(r.skips)
        out.append(
            TierSummary(
                tier=tier,
                latency_s=latency_s,
                wallets=len(rs),
                wallets_with_fills=len(filled),
                signals=sum(r.n_signals for r in rs),
                filled=sum(r.n_filled for r in rs),
                cost=cost,
                pnl=pnl,
                roi=pnl / cost if cost else 0.0,
                leader_roi=leader_pnl / leader_cost if leader_cost else 0.0,
                copy_efficiency=pnl / leader_pnl if leader_pnl > 0 else None,
                median_wallet_roi=statistics.median(r.roi for r in filled) if filled else None,
                share_wallets_profitable=sum(r.pnl > 0 for r in filled) / len(filled) if filled else None,
                mean_premium=sum(r.premium_sum for r in rs) / max(1, sum(r.n_filled for r in rs)),
                skips=dict(skips),
            )
        )
    return out


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


def walk_forward_scores(
    db: Any, wallets: list[str], *, split_ts: int, gap_days: float, thresholds: Thresholds | None = None
):
    meta = load_meta(db)
    scores = score_wallets(db, wallets, as_of=split_ts - int(gap_days * 86400), thresholds=thresholds, meta=meta)
    return meta, scores


def run_simulations(
    db: Any,
    meta: Meta,
    scores: Sequence[WalletScore],
    *,
    start: int,
    end: int,
    latencies: Sequence[int],
    base: CopyParams,
    price_book: PriceFn,
) -> list[tuple[WalletScore, CopyResult]]:
    results = []
    for s in scores:
        rows = db.activity(s.wallet, until=end)
        for latency in latencies:
            params = CopyParams(**(asdict(base) | {"latency_s": latency}))
            results.append(
                (s, simulate_copy(rows, meta, price_book, start=start, end=end, params=params, wallet=s.wallet))
            )
    return results


def predictive_check(
    results: Sequence[tuple[WalletScore, CopyResult]], latency_s: int, min_fills: int = 5
) -> dict[str, Any]:
    """Does the pre-split edge estimate rank wallets by out-of-sample follower ROI?"""
    pairs = [(s.theta_shrunk, r.roi) for s, r in results if r.latency_s == latency_s and r.n_filled >= min_fills]
    return {
        "latency_s": latency_s,
        "wallets": len(pairs),
        "spearman_theta_vs_follower_roi": spearman([a for a, _ in pairs], [b for _, b in pairs]),
    }
