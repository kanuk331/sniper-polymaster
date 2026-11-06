"""Strict fast-trade filter (research iteration 2, item 3).

A trader is uncopyable when a follower reacting 10-60 s later either cannot get comparable prices or keeps none of
the edge after costs. Everything is measured on data before the scoring cutoff, on the trader's most recent signals
in markets already resolved by then, using the same signal definition and copy rules as the backtest:

- fast_runaway     too many signals are skipped at the decision latency because the price ran past the cap
- fast_roundtrip   the leader halves too many positions within `quick_exit_s` (a follower cannot mirror the exit)
- uncopyable_edge  in-sample follower ROI at the decision latency (after spread and fees) is not above `min_copy_roi`
- copy_unverified  too few priced / resolved signals to judge (wallet goes to the insufficient tier)

Thresholds are fixed a priori (docs/research/2026-11-06-iteration2-preregistration.md), not tuned on results.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from sniper.analytics import Meta, Row
from sniper.backtest import CopyParams, PriceBook, simulate_copy
from sniper.metrics import Lot


@dataclass(frozen=True)
class FastFilter:
    sample_signals: int = 150
    min_priced: int = 20
    min_closed: int = 20
    max_runaway_share: float = 0.30
    max_quick_exit_share: float = 0.20
    quick_exit_s: int = 600
    min_copy_roi: float = 0.0
    latencies: tuple[int, ...] = (10, 60)
    decision_latency: int = 60


@dataclass
class Sample:
    signals: list[tuple[int, str]]  # (signal_ts, token_id) of the sampled signals
    points: list[tuple[str, int]]  # (token, ts) whose prices the assessment needs
    start: int


@dataclass
class Copyability:
    wallet: str
    n_signals: int = 0
    n_priced: int = 0
    n_closed: int = 0
    runaway_share: dict[int, float] = field(default_factory=dict)
    copy_roi: dict[int, float | None] = field(default_factory=dict)
    median_delay_s: dict[int, float | None] = field(default_factory=dict)
    leader_roi: float | None = None
    quick_exit_share: float = 0.0
    flags: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        row = {
            k: getattr(self, k)
            for k in ("wallet", "n_signals", "n_priced", "n_closed", "leader_roi", "quick_exit_share")
        }
        for lat, v in self.runaway_share.items():
            row[f"runaway_{lat}s"] = v
        for lat, v in self.copy_roi.items():
            row[f"copy_roi_{lat}s"] = v
        for lat, v in self.median_delay_s.items():
            row[f"median_delay_{lat}s"] = v
        row["flags"] = ",".join(self.flags)
        return row


class _NoPrices:
    def point(self, token_id: str, ts: int) -> None:
        return None

    def __call__(self, token_id: str, ts: int) -> None:
        return None


def plan_sample(
    rows: Sequence[Row], meta: Meta, *, as_of: int, start: int, strategy: CopyParams, ff: FastFilter
) -> Sample:
    """Pick the most recent signals (before as_of, in markets resolved before as_of) and the prices they need."""
    params = CopyParams(**(strategy.__dict__ | {"max_signals": 10**9}))
    dry = simulate_copy(rows, meta, _NoPrices(), start=start, end=as_of, params=params, resolve_as_of=as_of)  # type: ignore[arg-type]
    resolved = [lot for lot in dry if meta.resolution(lot.condition_id, as_of) is not None]
    chosen = resolved[-ff.sample_signals :]
    if not chosen:
        return Sample([], [], as_of)
    first = chosen[0].signal_ts
    tokens = {lot.token_id for lot in chosen}
    conditions = {lot.condition_id for lot in chosen}
    points = [(lot.token_id, lot.signal_ts) for lot in chosen]
    for r in rows:
        if not first <= r["ts"] < as_of:
            continue
        if r["type"] == "TRADE" and r["side"] == "SELL" and r["token_id"] in tokens:
            points.append((r["token_id"], r["ts"]))
        elif r["type"] == "MERGE" and r["condition_id"] in conditions:
            points += [(t.token_id, r["ts"]) for t in (meta.by_condition.get(r["condition_id"]) or {}).values()]
    return Sample([(lot.signal_ts, lot.token_id) for lot in chosen], points, first)


def quick_exit_share(rows: Sequence[Row], signals: Sequence[tuple[int, str]], window_s: int) -> float:
    """Share of signals after which the leader's holding in that token fell to half or less within window_s."""
    if not signals:
        return 0.0
    wanted = {t for _, t in signals}
    holdings: dict[str, float] = defaultdict(float)
    timeline: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for r in rows:
        if r["type"] == "TRADE" and r["token_id"] in wanted:
            holdings[r["token_id"]] += r["size"] if r["side"] == "BUY" else -r["size"]
            timeline[r["token_id"]].append((r["ts"], holdings[r["token_id"]]))
    quick = 0
    for ts, token in signals:
        series = timeline.get(token, [])
        at_signal = max((h for t, h in series if t <= ts), default=0.0)
        if at_signal <= 0:
            continue
        if any(h <= at_signal / 2 for t, h in series if ts < t <= ts + window_s):
            quick += 1
    return quick / len(signals)


def assess(
    rows: Sequence[Row],
    meta: Meta,
    book: PriceBook,
    sample: Sample,
    *,
    wallet: str,
    as_of: int,
    strategy: CopyParams,
    ff: FastFilter,
) -> Copyability:
    c = Copyability(wallet=wallet, n_signals=len(sample.signals))
    if not sample.signals:
        c.flags.append("copy_unverified")
        return c
    wanted = set(sample.signals)
    for lat in ff.latencies:
        params = CopyParams(**(strategy.__dict__ | {"latency_s": lat, "max_signals": 10**9}))
        lots = [
            lot
            for lot in simulate_copy(
                rows, meta, book, start=sample.start, end=as_of, params=params, wallet=wallet, resolve_as_of=as_of
            )
            if (lot.signal_ts, lot.token_id) in wanted
        ]
        priced = [lot for lot in lots if lot.status != "skip:no_price"]
        closed = [lot for lot in priced if lot.filled and lot.open_cost < 1e-9]
        cost = sum(lot.cost for lot in closed)
        c.runaway_share[lat] = sum(lot.status == "skip:premium" for lot in priced) / len(priced) if priced else 0.0
        c.copy_roi[lat] = sum(lot.pnl for lot in closed) / cost if cost else None
        delays = [lot.exec_ts - lot.signal_ts for lot in priced if lot.exec_ts]
        c.median_delay_s[lat] = statistics.median(delays) if delays else None
        if lat == ff.decision_latency:
            c.n_priced, c.n_closed = len(priced), len(closed)
            c.leader_roi = _leader_roi(lots)
    c.quick_exit_share = quick_exit_share(rows, sample.signals, ff.quick_exit_s)
    c.flags = copyability_flags(c, ff)
    return c


def _leader_roi(lots: Sequence[Lot]) -> float | None:
    cost = sum(lot.bench_cost for lot in lots)
    return sum(lot.bench_pnl for lot in lots) / cost if cost else None


def copyability_flags(c: Copyability, ff: FastFilter) -> list[str]:
    flags = []
    lat = ff.decision_latency
    if c.n_priced >= ff.min_priced and c.runaway_share.get(lat, 0.0) >= ff.max_runaway_share:
        flags.append("fast_runaway")
    if c.n_signals >= ff.min_priced and c.quick_exit_share >= ff.max_quick_exit_share:
        flags.append("fast_roundtrip")
    roi = c.copy_roi.get(lat)
    if c.n_closed >= ff.min_closed and roi is not None and roi <= ff.min_copy_roi:
        flags.append("uncopyable_edge")
    if c.n_priced < ff.min_priced or c.n_closed < ff.min_closed:
        flags.append("copy_unverified")
    return flags
