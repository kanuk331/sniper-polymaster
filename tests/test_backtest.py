import pytest

from sniper.analytics import MarketMeta, Meta, TokenMeta
from sniper.backtest import (
    CopyParams,
    LotLabels,
    PriceBook,
    aggregate_orders,
    choose_wallets_to_simulate,
    predictive_check,
    simulate_copy,
    spearman,
)
from sniper.fees import FEE_V2_START, taker_fee
from sniper.metrics import summarize

T0 = FEE_V2_START + 86400  # fees in force


def meta(y0: float | None = 1.0, fee: float = 0.04, closed: int = T0 + 10_000) -> Meta:
    tokens = {
        f"a-{i}": TokenMeta(
            f"a-{i}", "a", i, y0 is not None, None if y0 is None else (y0 if i == 0 else 1 - y0), "ev:a", False
        )
        for i in (0, 1)
    }
    return Meta(tokens, {"a": MarketMeta("a", "politics", fee, closed)})


def trade(dt, side="BUY", price=0.40, size=100.0, token="a-0", tx=None):
    return {
        "ts": T0 + dt,
        "type": "TRADE",
        "side": side,
        "price": price,
        "size": size,
        "token_id": token,
        "condition_id": "a",
        "usdc_size": price * size,
        "tx_hash": tx or f"0x{dt}{side}{price}",
    }


class Flat:
    """Price book returning a constant price, stamped `lag` seconds after the requested time."""

    def __init__(self, price, lag=0):
        self.price, self.lag = price, lag

    def point(self, token, ts):
        return None if self.price is None else (ts + self.lag, self.price)

    def __call__(self, token, ts):
        return self.price


def sim(rows, m, book, **kw):
    params = CopyParams(**{"max_premium": 0.05} | kw)
    return simulate_copy(rows, m, book, start=T0, end=T0 + 5000, params=params, wallet="w", labels=LotLabels("p"))


def test_price_book_lookup():
    book = PriceBook({"t": [(100, 0.4), (160, 0.5)]}, max_wait_s=30)
    assert book.point("t", 90) == (100, 0.4)
    assert book("t", 101) is None  # next point is 59 s away
    assert book("t", 150) == 0.5
    assert book("t", 200) is None and book("x", 100) is None


def test_filled_lot_pays_spread_fee_and_settles():
    [lot] = sim([trade(10)], meta(1.0), Flat(0.40, lag=30), half_spread=0.01, max_premium=0.02)
    price, shares = 0.41, 100 / 0.41
    assert lot.filled and lot.exec_ts == T0 + 10 + 60 + 30 and lot.ref_price == 0.40
    assert lot.entry_fee == pytest.approx(taker_fee(shares, price, 0.04))
    assert lot.pnl == pytest.approx(shares - 100 - lot.entry_fee)
    assert lot.bench_pnl == pytest.approx(100 / 0.40 - 100)
    assert lot.slippage_usd == pytest.approx(0.01 * shares)
    assert lot.close_ts == T0 + 10_000
    assert lot.flows == [(lot.exec_ts, -lot.entry_fee, 100), (T0 + 10_000, shares - 100, -100)]


def test_no_fees_before_fee_regime():
    early = meta(1.0)
    rows = [dict(trade(10), ts=FEE_V2_START - 86400 * 30)]
    lots = simulate_copy(rows, early, Flat(0.40), start=0, end=FEE_V2_START, params=CopyParams(max_premium=0.05))
    assert lots[0].entry_fee == 0.0


def test_skip_reasons_and_missed_benchmark():
    [a] = sim([trade(10)], meta(1.0), Flat(0.43), half_spread=0.01, max_premium=0.015)
    assert a.status == "skip:premium" and a.pnl == 0.0
    assert a.bench_pnl == pytest.approx(100 / 0.40 - 100)  # what the leader made on the missed signal
    [b] = sim([trade(10)], meta(), Flat(None))
    assert b.status == "skip:no_price"
    [c] = sim([trade(10, price=0.985)], meta(), Flat(0.975), half_spread=0.01)
    assert c.status == "skip:extreme_price"


def test_history_sizes_proportional_exit_and_follower_capped_at_leader():
    rows = [trade(-500, size=100), trade(10, size=100), trade(1500, side="SELL", price=0.30, size=100)]
    [lot] = sim(rows, meta(0.0), Flat(0.42), half_spread=0.0)
    # entry at max(0.42, 0.40); leader held 200 and sold 100 -> half exits at min(0.42, 0.30); rest loses
    shares = 100 / 0.42
    assert lot.exit_proceeds == pytest.approx(shares / 2 * 0.30)
    assert lot.settle_value == 0.0
    assert lot.pnl == pytest.approx(shares / 2 * 0.30 - 100 - lot.fees)
    assert sum(dc for _, _, dc in lot.flows) == pytest.approx(0.0)  # all capital returned or written off
    assert sum(dp for _, dp, _ in lot.flows) == pytest.approx(lot.pnl)


def test_unresolved_marked_at_cost():
    [lot] = sim([trade(10)], meta(None, fee=0.0), Flat(0.4), half_spread=0.0)
    assert lot.pnl == pytest.approx(0.0) and lot.open_cost == pytest.approx(100.0)
    assert lot.bench_pnl == pytest.approx(0.0)


def test_resolve_as_of_hides_later_outcomes():
    rows = [trade(10)]
    lots = simulate_copy(
        rows, meta(1.0, closed=T0 + 10_000), Flat(0.4), start=T0, end=T0 + 5000,
        params=CopyParams(max_premium=0.05), resolve_as_of=T0 + 5000,
    )  # fmt: skip
    assert lots[0].open_cost > 0 and lots[0].settle_value == 0.0


def test_book_sweep_is_one_signal_priced_at_worst_level():
    sweep = [trade(10, price=p, tx="0xs") for p in (0.30, 0.32, 0.34)]
    [order] = aggregate_orders(sweep)
    assert order["size"] == 300 and order["price"] == pytest.approx(0.32) and order["worst_price"] == 0.34
    [lot] = sim(sweep, meta(1.0, fee=0.0), Flat(0.30), half_spread=0.01, max_premium=0.03)
    assert lot.exec_price == 0.34 and lot.pnl == pytest.approx(100 / 0.34 - 100)


def test_max_signals_cap_and_summary_consistency():
    rows = [trade(10 + i, tx=f"0x{i}") for i in range(10)]
    lots = sim(rows, meta(1.0), Flat(0.4), max_signals=3, half_spread=0.0)
    assert len(lots) == 3
    s = summarize(lots, n_boot=50)
    assert s["filled"] == 3 and s["cost"] == 300 and s["pnl"] == pytest.approx(sum(lot.pnl for lot in lots))
    assert s["peak_capital"] == 300


def test_spearman():
    assert spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 2], [1, 2]) is None


def test_selection_samples_each_exclusion_reason_and_predictive_check():
    from sniper.analytics import Features, SkillStats
    from sniper.scoring import WalletScore

    f = Features(1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0.5, 0, 0, 0, 0, 0, 0, 0, {})
    k = SkillStats(0, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0)
    tiers = [("candidate", ""), ("noise", ""), ("noise", ""), ("excluded", "mm_or_arb"), ("excluded", "fast_runaway")]
    scores = [WalletScore(f"w{i}", None, f, k, [], tier=t, style=st) for i, (t, st) in enumerate(tiers)]
    chosen = choose_wallets_to_simulate(scores, n_noise=1, n_per_exclusion=1)
    assert [s.tier for s in chosen].count("noise") == 1
    assert {s.style for s in chosen if s.tier == "excluded"} == {"mm_or_arb", "fast_runaway"}
    lots = [lot for i in range(6) for lot in sim([trade(10 + i, tx=f"0x{i}")], meta(1.0), Flat(0.4))]
    for lot in lots:
        lot.wallet = "w0"
    assert predictive_check(lots, scores, latency_s=60)["wallets"] == 1
