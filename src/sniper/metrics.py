"""Copy-trade ledger and performance metrics shared by the backtest and paper trading.

Every leader BUY order the follower could have copied becomes one `Lot`, filled or skipped. Metrics are computed
from lots, so backtests and live paper trading report the same quantities: trade counts, missed trades, capital
requirement, realized-basis drawdown, fees, slippage, ROI with an event-clustered bootstrap confidence interval.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, fields
from typing import Any

import numpy as np

FILLED = "filled"


@dataclass
class Lot:
    period: str
    strategy: str
    wallet: str
    user_name: str
    tier: str
    style: str
    category: str
    event_key: str
    condition_id: str
    token_id: str
    latency_s: int
    signal_ts: int
    leader_price: float  # leader order VWAP
    leader_worst: float  # worst level the leader's order reached
    leader_notional: float
    status: str = FILLED  # FILLED or "skip:<reason>"
    exec_ts: int | None = None  # when the follower executed (price bar / book snapshot time)
    ref_price: float | None = None  # observed price at exec time before spread (bar price or book mid)
    exec_price: float | None = None
    shares: float = 0.0
    cost: float = 0.0
    entry_fee: float = 0.0
    exit_proceeds: float = 0.0
    exit_fee: float = 0.0
    settle_value: float = 0.0
    open_cost: float = 0.0  # cost basis still open at the end of the evaluation (marked at cost: zero PnL)
    close_ts: int | None = None
    bench_cost: float = 0.0  # leader benchmark: same stake at the leader's VWAP, no fees, leader's exits
    bench_value: float = 0.0
    # (ts, realized PnL change, deployed capital change); built by the simulator / paper engine
    flows: list[tuple[int, float, float]] = field(default_factory=list)

    @property
    def filled(self) -> bool:
        return self.status == FILLED

    @property
    def fees(self) -> float:
        return self.entry_fee + self.exit_fee

    @property
    def pnl(self) -> float:
        if not self.filled:
            return 0.0
        return self.exit_proceeds + self.settle_value + self.open_cost - self.cost - self.fees

    @property
    def bench_pnl(self) -> float:
        return self.bench_value - self.bench_cost

    @property
    def slippage_usd(self) -> float:
        """Extra paid versus the leader's VWAP for the shares bought (positive = worse than the leader)."""
        if not self.filled or self.exec_price is None:
            return 0.0
        return (self.exec_price - self.leader_price) * self.shares

    def as_row(self) -> dict[str, Any]:
        row = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "flows"}
        row |= {"pnl": self.pnl, "fees": self.fees, "slippage_usd": self.slippage_usd, "bench_pnl": self.bench_pnl}
        return row


# --- bootstrap ---------------------------------------------------------------------------------------------------


def cluster_bootstrap_ratio(
    numer: Sequence[float],
    denom: Sequence[float],
    clusters: Sequence[str],
    *,
    n_boot: int = 1000,
    alpha: float = 0.05,
    seed: int = 0,
    chunk: int = 100,
) -> tuple[float, float] | None:
    """Percentile CI of sum(numer)/sum(denom), resampling whole clusters (events) with replacement."""
    num_c: dict[str, float] = defaultdict(float)
    den_c: dict[str, float] = defaultdict(float)
    for n, d, c in zip(numer, denom, clusters, strict=True):
        num_c[c] += n
        den_c[c] += d
    keys = list(num_c)
    k = len(keys)
    if k < 2:
        return None
    nums = np.array([num_c[c] for c in keys])
    dens = np.array([den_c[c] for c in keys])
    rng = np.random.default_rng(seed)
    stats = []
    for start in range(0, n_boot, chunk):
        size = min(chunk, n_boot - start)
        counts = rng.multinomial(k, np.full(k, 1.0 / k), size=size)
        d = counts @ dens
        with np.errstate(divide="ignore", invalid="ignore"):
            stats.append(np.where(d > 0, (counts @ nums) / d, np.nan))
    arr = np.concatenate(stats)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return None
    return float(np.quantile(arr, alpha / 2)), float(np.quantile(arr, 1 - alpha / 2))


# --- capital and drawdown ----------------------------------------------------------------------------------------


def capital_and_drawdown(lots: Iterable[Lot]) -> dict[str, float]:
    """Peak deployed capital and max drawdown of the realized-PnL curve (open positions carried at cost)."""
    events = sorted((ts, dp, dc) for lot in lots if lot.filled for ts, dp, dc in lot.flows)
    capital = peak_capital = 0.0
    equity = peak_equity = max_dd = 0.0
    for _, dp, dc in events:
        capital += dc
        peak_capital = max(peak_capital, capital)
        equity += dp
        peak_equity = max(peak_equity, equity)
        max_dd = max(max_dd, peak_equity - equity)
    return {
        "peak_capital": peak_capital,
        "max_drawdown": max_dd,
        "max_drawdown_pct_capital": max_dd / peak_capital if peak_capital else 0.0,
    }


# --- summaries ---------------------------------------------------------------------------------------------------


def summarize(lots: Sequence[Lot], *, n_boot: int = 1000, seed: int = 0) -> dict[str, Any]:
    filled = [lot for lot in lots if lot.filled]
    skipped = [lot for lot in lots if not lot.filled]
    cost = sum(lot.cost for lot in filled)
    pnl = sum(lot.pnl for lot in filled)
    shares = sum(lot.shares for lot in filled)
    bench_cost = sum(lot.bench_cost for lot in filled)
    closed = [lot for lot in filled if lot.open_cost < 1e-9]
    ci = cluster_bootstrap_ratio(
        [lot.pnl for lot in filled], [lot.cost for lot in filled], [lot.event_key for lot in filled],
        n_boot=n_boot, seed=seed,
    )  # fmt: skip
    hold = [(lot.close_ts - lot.exec_ts) / 86400 for lot in closed if lot.close_ts and lot.exec_ts]
    out = {
        "signals": len(lots),
        "filled": len(filled),
        "missed": len(skipped),
        "missed_share": len(skipped) / len(lots) if lots else 0.0,
        "missed_by_reason": dict(Counter(lot.status.removeprefix("skip:") for lot in skipped)),
        "missed_leader_pnl": sum(lot.bench_pnl for lot in skipped),
        "wallets": len({lot.wallet for lot in lots}),
        "wallets_filled": len({lot.wallet for lot in filled}),
        "events": len({lot.event_key for lot in filled}),
        "cost": cost,
        "fees": sum(lot.fees for lot in filled),
        "slippage_usd": sum(lot.slippage_usd for lot in filled),
        "slippage_cents": 100 * sum(lot.slippage_usd for lot in filled) / shares if shares else 0.0,
        "pnl": pnl,
        "roi": pnl / cost if cost else 0.0,
        "roi_ci95": ci,
        "leader_roi": (sum(lot.bench_pnl for lot in filled) / bench_cost) if bench_cost else 0.0,
        "win_rate": sum(lot.pnl > 0 for lot in closed) / len(closed) if closed else None,
        "open_share": sum(lot.open_cost for lot in filled) / cost if cost else 0.0,
        "median_hold_days": float(np.median(hold)) if hold else None,
    }
    return out | capital_and_drawdown(filled)


def breakdown(
    lots: Sequence[Lot], keys: Sequence[str], *, n_boot: int = 1000, min_signals: int = 1
) -> list[dict[str, Any]]:
    """summarize() per combination of Lot attributes, e.g. keys=("tier", "latency_s")."""
    groups: dict[tuple, list[Lot]] = defaultdict(list)
    for lot in lots:
        groups[tuple(getattr(lot, k) for k in keys)].append(lot)
    rows = []
    for key, group in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        if len(group) < min_signals:
            continue
        rows.append(dict(zip(keys, key, strict=True)) | summarize(group, n_boot=n_boot))
    return rows


def filter_lots(lots: Iterable[Lot], pred: Callable[[Lot], bool]) -> list[Lot]:
    return [lot for lot in lots if pred(lot)]


def lot_from_row(row: dict[str, Any]) -> Lot:
    names = {f.name for f in fields(Lot)}
    return Lot(**{k: v for k, v in row.items() if k in names})


def lots_to_rows(lots: Iterable[Lot]) -> list[dict[str, Any]]:
    return [lot.as_row() for lot in lots]


def fmt_ci(ci: tuple[float, float] | None, pct: bool = True) -> str:
    if ci is None:
        return "n/a"
    lo, hi = ci
    return f"[{lo:+.1%}, {hi:+.1%}]" if pct else f"[{lo:+.3f}, {hi:+.3f}]"


__all__ = [
    "FILLED",
    "Lot",
    "asdict",
    "breakdown",
    "capital_and_drawdown",
    "cluster_bootstrap_ratio",
    "filter_lots",
    "fmt_ci",
    "lot_from_row",
    "lots_to_rows",
    "math",
    "summarize",
]
