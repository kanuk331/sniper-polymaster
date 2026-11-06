import pytest

from sniper.analytics import MarketMeta, Meta, TokenMeta
from sniper.backtest import (
    CopyParams,
    PriceBook,
    choose_wallets_to_simulate,
    simulate_copy,
    spearman,
    summarize,
    taker_fee,
)


def meta(y0: float | None = 1.0, fee: float = 0.04) -> Meta:
    tokens = {
        f"a-{i}": TokenMeta(
            f"a-{i}", "a", i, y0 is not None, None if y0 is None else (y0 if i == 0 else 1 - y0), "ev:a", False
        )
        for i in (0, 1)
    }
    return Meta(tokens, {"a": MarketMeta("a", "politics", fee, 10)})


def trade(ts, side="BUY", price=0.40, size=100.0, token="a-0", tx=None):
    return {
        "ts": ts,
        "type": "TRADE",
        "side": side,
        "price": price,
        "size": size,
        "token_id": token,
        "condition_id": "a",
        "usdc_size": price * size,
        "tx_hash": tx or f"0x{ts}{side}{price}",
    }


def flat(price: float):
    return lambda token, ts: price


def test_price_book_lookup():
    book = PriceBook({"t": [(100, 0.4), (160, 0.5)]}, max_wait_s=30)
    assert book("t", 90) == 0.4
    assert book("t", 101) is None  # next point is 59 s away
    assert book("t", 150) == 0.5
    assert book("t", 200) is None
    assert book("x", 100) is None


def test_follower_pays_spread_and_fee_and_wins_at_resolution():
    p = CopyParams(latency_s=60, stake_usdc=100, half_spread=0.01, max_premium=0.02)
    r = simulate_copy([trade(1000)], meta(1.0), flat(0.40), start=0, end=2000, params=p)
    price = 0.41
    shares = 100 / price
    assert r.n_filled == 1
    assert r.fees == pytest.approx(taker_fee(shares, price, 0.04))
    assert r.pnl == pytest.approx(shares - 100 - r.fees)
    assert r.leader_pnl == pytest.approx(100 / 0.40 - 100)
    assert r.pnl < r.leader_pnl


def test_signal_skipped_when_price_ran_away_or_missing():
    p = CopyParams(max_premium=0.015, half_spread=0.01)
    r = simulate_copy([trade(1000)], meta(), flat(0.43), start=0, end=2000, params=p)
    assert r.n_filled == 0 and r.skips["premium"] == 1
    r = simulate_copy([trade(1000)], meta(), lambda t, ts: None, start=0, end=2000, params=p)
    assert r.skips["no_price"] == 1
    r = simulate_copy([trade(1000, price=0.985)], meta(), flat(0.975), start=0, end=2000, params=p)
    assert r.skips["extreme_price"] == 1


def test_only_signals_inside_window_are_copied_but_history_sizes_exits():
    rows = [trade(10, size=100), trade(1000, size=100), trade(1500, side="SELL", price=0.6, size=100)]
    p = CopyParams(half_spread=0.0, max_premium=0.05)
    r = simulate_copy(rows, meta(0.0), flat(0.4), start=500, end=2000, params=p)
    assert r.n_signals == 1
    # leader held 200 before selling 100 -> follower exits half; the rest loses at resolution
    shares = 100 / 0.4
    assert r.proceeds == pytest.approx(shares / 2 * 0.4)
    assert r.settled == 0.0


def test_unresolved_positions_marked_at_cost():
    p = CopyParams(half_spread=0.0, max_premium=0.05)
    r = simulate_copy([trade(1000)], meta(None, fee=0.0), flat(0.4), start=0, end=2000, params=p)
    assert r.pnl == pytest.approx(0.0)
    assert r.leader_pnl == pytest.approx(0.0)
    assert r.open_cost == pytest.approx(100.0)


def test_max_signals_cap():
    rows = [trade(1000 + i) for i in range(10)]
    r = simulate_copy(rows, meta(), flat(0.4), start=0, end=5000, params=CopyParams(max_signals=3, max_premium=0.05))
    assert r.n_signals == 3


def test_spearman():
    assert spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 2], [1, 2]) is None


def test_summarize_and_selection():
    from sniper.analytics import Features, SkillStats
    from sniper.scoring import WalletScore

    f = Features(1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0.5, 0, 0, 0, 0, 0, 0, 0, {})
    k = SkillStats(0, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0)
    scores = [
        WalletScore(f"w{i}", None, f, k, [], tier=t) for i, t in enumerate(["candidate", "noise", "noise", "excluded"])
    ]
    chosen = choose_wallets_to_simulate(scores, include_excluded=1, include_noise=1)
    assert [s.tier for s in chosen].count("noise") == 1 and len(chosen) == 3
    res = simulate_copy([trade(1000)], meta(1.0), flat(0.4), start=0, end=2000, params=CopyParams(max_premium=0.05))
    [summary] = summarize([(scores[0], res)], latency_s=60)
    assert summary.tier == "candidate" and summary.filled == 1 and summary.roi == pytest.approx(res.roi)


def test_follower_never_buys_cheaper_or_sells_dearer_than_leader():
    p = CopyParams(half_spread=0.01, max_premium=0.05)
    # stale history price 0.10 right after the leader bought at 0.60: follower pays the leader's price
    r = simulate_copy([trade(1000, price=0.60)], meta(1.0), flat(0.10), start=0, end=2000, params=p)
    assert r.n_filled == 1 and r.mean_premium == pytest.approx(0.0)
    assert r.pnl == pytest.approx(100 / 0.60 - 100 - r.fees)
    # entry at max(0.42 + 0.01, 0.40) = 0.43; the leader dumps at 0.30 -> follower exits at min(0.41, 0.30)
    rows = [trade(1000, price=0.40), trade(1100, side="SELL", price=0.30, size=100)]
    r = simulate_copy(rows, meta(0.0), flat(0.42), start=0, end=2000, params=p)
    assert r.n_filled == 1
    assert r.proceeds == pytest.approx(100 / 0.43 * 0.30)


def test_book_sweep_is_one_signal_priced_at_worst_level():
    from sniper.backtest import aggregate_orders

    sweep = [
        trade(1000, price=0.03, size=100, tx="0xs"),
        trade(1000, price=0.05, size=100, tx="0xs"),
        trade(1000, price=0.07, size=100, tx="0xs"),
    ]
    [order] = aggregate_orders(sweep)
    assert order["size"] == 300 and order["price"] == pytest.approx(0.05) and order["worst_price"] == 0.07
    p = CopyParams(half_spread=0.01, max_premium=0.03, min_price=0.0)
    r = simulate_copy(sweep, meta(1.0, fee=0.0), flat(0.03), start=0, end=2000, params=p)
    assert r.n_signals == 1 and r.n_filled == 1
    assert r.pnl == pytest.approx(100 / 0.07 - 100)  # not 100 / 0.04: the cheap levels are gone
