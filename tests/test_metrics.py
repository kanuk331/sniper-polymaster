import numpy as np
import pytest

from sniper.metrics import Lot, breakdown, capital_and_drawdown, cluster_bootstrap_ratio, summarize


def lot(event="e1", status="filled", cost=100.0, pnl=10.0, latency=60, wallet="w", flows=None, **kw) -> Lot:
    base = dict(
        period="p",
        strategy="baseline",
        wallet=wallet,
        user_name="",
        tier="candidate",
        style="balanced",
        category="politics",
        event_key=event,
        condition_id="c",
        token_id="t",
        latency_s=latency,
        signal_ts=0,
        leader_price=0.40,
        leader_worst=0.40,
        leader_notional=1000.0,
        status=status,
    )
    if status != "filled":
        return Lot(**(base | kw))
    shares = cost / 0.42
    return Lot(
        **(
            base
            | dict(
                exec_ts=10,
                ref_price=0.41,
                exec_price=0.42,
                shares=shares,
                cost=cost,
                settle_value=cost + pnl,
                close_ts=10 + 86400,
                bench_cost=cost,
                bench_value=cost + pnl + 5,
                flows=flows or [],
            )
            | kw
        )
    )


def test_lot_pnl_slippage_and_skips():
    filled = lot(pnl=10.0, entry_fee=1.0)
    assert filled.pnl == pytest.approx(9.0)
    assert filled.slippage_usd == pytest.approx(0.02 * 100 / 0.42)
    skipped = lot(status="skip:premium", bench_cost=100.0, bench_value=130.0)
    assert skipped.pnl == 0.0 and skipped.bench_pnl == 30.0


def test_summarize_counts_missed_and_roi():
    lots = [lot("e1", pnl=20), lot("e2", pnl=-10), lot("e3", status="skip:premium", bench_cost=100, bench_value=150)]
    s = summarize(lots, n_boot=200)
    assert (s["signals"], s["filled"], s["missed"]) == (3, 2, 1)
    assert s["missed_by_reason"] == {"premium": 1}
    assert s["missed_leader_pnl"] == 50
    assert s["roi"] == pytest.approx(10 / 200)
    assert s["win_rate"] == 0.5
    lo, hi = s["roi_ci95"]
    assert lo <= s["roi"] <= hi


def test_bootstrap_ci_contains_truth_and_clusters_widen_it():
    rng = np.random.default_rng(1)
    pnl = rng.normal(5, 50, 400)
    cost = np.full(400, 100.0)
    lo, hi = cluster_bootstrap_ratio(pnl, cost, [str(i) for i in range(400)], n_boot=500)
    assert lo < 0.05 < hi
    # same data but grouped into 20 perfectly correlated events -> wider interval
    pnl_c = np.repeat(rng.normal(5, 50, 20), 20)
    lo_c, hi_c = cluster_bootstrap_ratio(pnl_c, cost, [str(i // 20) for i in range(400)], n_boot=500)
    assert hi_c - lo_c > hi - lo
    assert cluster_bootstrap_ratio([1.0], [1.0], ["a"]) is None


def test_capital_and_drawdown_from_flows():
    a = lot(flows=[(0, 0.0, 100.0), (10, -100.0, -100.0)])  # bought, then lost everything
    b = lot(flows=[(5, 0.0, 100.0), (20, 150.0, -100.0)])  # bought, then won 150
    c = lot(flows=[(1, -2.0, 0.0)])  # fee at entry
    m = capital_and_drawdown([a, b, c])
    assert m["peak_capital"] == 200.0
    assert m["max_drawdown"] == pytest.approx(102.0)
    assert m["max_drawdown_pct_capital"] == pytest.approx(0.51)


def test_breakdown_groups():
    lots = [lot(latency=10), lot(latency=60), lot(latency=60, wallet="x")]
    rows = breakdown(lots, ["latency_s"], n_boot=50)
    assert [(r["latency_s"], r["filled"], r["wallets"]) for r in rows] == [(10, 1, 1), (60, 2, 2)]
