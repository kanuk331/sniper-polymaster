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


def test_lots_csv_roundtrip_keeps_flows(tmp_path):
    from sniper.walkforward import read_lots, write_lots

    lots = [lot(flows=[(5, -1.0, 100.0), (50, 20.0, -100.0)]), lot(status="skip:premium", bench_cost=100.0)]
    write_lots(lots, tmp_path / "lots.csv.gz")
    back = read_lots(tmp_path / "lots.csv.gz")
    assert [x.status for x in back] == ["filled", "skip:premium"]
    assert back[0].flows == [(5, -1.0, 100.0), (50, 20.0, -100.0)]
    assert back[0].pnl == pytest.approx(lots[0].pnl) and back[1].exec_ts is None


def test_holdout_guard(tmp_path, monkeypatch):
    from sniper import walkforward
    from sniper.plan import Period

    dev, hold = Period("d", "2026-01-01", "dev"), Period("h", "2026-01-01", "holdout")
    monkeypatch.setattr(walkforward, "git_state", lambda: ("abc123", True))
    assert walkforward.holdout_guard(dev, tmp_path, confirm=False, allow_rerun=False) == "abc123"
    with pytest.raises(SystemExit, match="confirm-holdout"):
        walkforward.holdout_guard(hold, tmp_path, confirm=False, allow_rerun=False)
    with pytest.raises(SystemExit, match="frozen code"):
        walkforward.holdout_guard(hold, tmp_path, confirm=True, allow_rerun=False)
    monkeypatch.setattr(walkforward, "git_state", lambda: ("abc123", False))
    assert walkforward.holdout_guard(hold, tmp_path, confirm=True, allow_rerun=False) == "abc123"
    (tmp_path / "HOLDOUT_EVALUATED").write_text("commit abc123")
    with pytest.raises(SystemExit, match="already evaluated"):
        walkforward.holdout_guard(hold, tmp_path, confirm=True, allow_rerun=False)


def _write_period(root, name, lots_):
    import json

    from sniper.walkforward import write_lots

    d = root / name
    d.mkdir(parents=True)
    summary = {"universe": {"wallets": 10, "status": {"complete": 10}}, "score_tiers": {"tiers": {"noise": 5}},
               "copyability_flags": {}, "commit": "abcdef123456", "predictive": []}  # fmt: skip
    (d / "summary.json").write_text(json.dumps(summary))
    write_lots(lots_, d / "lots.csv.gz")


def test_report_decision_h1_h2(tmp_path):
    from sniper import walkforward
    from sniper.plan import Period

    periods = [Period("pa", "2025-01-01", "holdout"), Period("pb", "2025-04-01", "holdout")]

    def run(root, pnls):
        for p in periods:
            noise = [
                lot(f"{p.name}-e{i}", pnl=x, period=p.name, strategy="position", tier="noise", wallet=f"w{i % 5}")
                for i, x in enumerate(pnls)
            ]
            _write_period(root, p.name, noise + [lot(f"{p.name}-b", pnl=99.0, period=p.name, strategy="baseline")])
        return walkforward.report(periods, root, label="t", primary="position", decision_latency=60, n_boot=200)

    d = run(tmp_path / "edge", [30.0] * 40 + [-20.0] * 10)["decision"]
    h2 = d["h2_eligible"]
    assert d["h1_candidate"] is None  # the only candidate lots belong to the non-primary strategy
    assert (h2["filled"], h2["events"], h2["wallets"]) == (100, 100, 5)
    assert h2["level"] == pytest.approx(0.975) and h2["roi"] == pytest.approx(0.2)
    assert h2["supported"] and d["supported"]
    md = (tmp_path / "edge" / "report-t.md").read_text()
    assert "H2, candidate + watch + noise" in md and "→ **подтверждено**" in md

    d = run(tmp_path / "flat", [20.0, -20.0] * 25)["decision"]
    assert d["h2_eligible"]["roi"] == pytest.approx(0.0)
    assert not d["h2_eligible"]["supported"] and not d["supported"]
