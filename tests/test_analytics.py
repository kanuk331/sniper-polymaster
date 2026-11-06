import math

import numpy as np
import pytest

from sniper.analytics import (
    Bet,
    MarketMeta,
    Meta,
    TokenMeta,
    bh_qvalues,
    build_bets,
    empirical_bayes,
    event_variance,
    features,
    logit_offset_mle,
    resolved_condition_pnl,
    skill_stats,
)


def meta_for(conditions: dict[str, tuple[float | None, int | None]], *, neg_risk_event: str | None = None) -> Meta:
    """conditions: cid -> (final price of outcome 0 or None, closed_time). Tokens are '<cid>-0' / '<cid>-1'."""
    tokens, markets = {}, {}
    for cid, (fp0, closed) in conditions.items():
        for i in (0, 1):
            fp = None if fp0 is None else (fp0 if i == 0 else 1 - fp0)
            key = f"nr:{neg_risk_event}" if neg_risk_event else f"ev:{cid}"
            tokens[f"{cid}-{i}"] = TokenMeta(f"{cid}-{i}", cid, i, fp0 is not None, fp, key, neg_risk_event is not None)
        markets[cid] = MarketMeta(cid, "politics", 0.04, closed)
    return Meta(tokens, markets)


def row(ts, token, side="BUY", price=0.4, size=10.0, kind="TRADE", usdc=None, condition=""):
    return {
        "ts": ts,
        "type": kind,
        "side": side,
        "token_id": token,
        "price": price,
        "size": size,
        "usdc_size": usdc if usdc is not None else price * size,
        "condition_id": condition,
        "slug": "m",
    }


def test_event_variance_single_and_mutually_exclusive():
    assert event_variance([(1.0, 0.3)], mutually_exclusive=False) == pytest.approx(0.21)
    # two exclusive Yes bets at 0.3: Var = 0.6 - 0.36
    assert event_variance([(1.0, 0.3), (1.0, 0.3)], mutually_exclusive=True) == pytest.approx(0.24)
    # unknown correlation: perfect-correlation upper bound
    s = math.sqrt(0.21)
    assert event_variance([(1.0, 0.3), (1.0, 0.3)], mutually_exclusive=False) == pytest.approx((2 * s) ** 2)


def test_event_variance_rescales_overpriced_entries():
    v = event_variance([(1.0, 0.7), (1.0, 0.7)], mutually_exclusive=True)
    assert v == pytest.approx(1.0 - 1.0)  # rescaled to 0.5 + 0.5: one of them surely wins


def test_logit_offset_mle_recovers_edge():
    rng = np.random.default_rng(7)
    pis = rng.uniform(0.1, 0.9, 20000)
    true = 0.3
    ys = (rng.uniform(size=pis.size) < 1 / (1 + np.exp(-(np.log(pis / (1 - pis)) + true)))).astype(float)
    theta, se = logit_offset_mle(pis, ys)
    assert abs(theta - true) < 3 * se
    assert se == pytest.approx(1 / math.sqrt(np.sum(pis * (1 - pis))), rel=0.2)


def test_bh_and_by_qvalues():
    q = bh_qvalues([0.01, 0.04, 0.03, 0.5])
    assert q == pytest.approx([0.04, 0.04 * 4 / 3, 0.04 * 4 / 3, 0.5])
    q_by = bh_qvalues([0.01, 0.04, 0.03, 0.5], dependent=True)
    assert all(b >= a for a, b in zip(q, q_by, strict=True))
    assert bh_qvalues([]) == []


def test_empirical_bayes_shrinks_noisy_estimates_more():
    thetas = [0.8, 0.8, -0.6, 0.4, -0.2, 0.5]
    ses = [0.05, 1.0, 0.05, 0.05, 0.05, 0.05]
    out = empirical_bayes(thetas, ses)
    mu = out[0].prior_mean
    assert abs(out[0].mean - mu) > abs(out[1].mean - mu)
    assert out[1].sd < 1.0


def test_build_bets_respects_cutoff_and_resolution_time():
    meta = meta_for({"a": (1.0, 500), "b": (0.0, 2000)})
    rows = [row(100, "a-0", price=0.4), row(110, "a-0", price=0.6), row(120, "b-1", price=0.7), row(1500, "b-0")]
    bets = {b.condition_id: b for b in build_bets(rows, meta, as_of=1000)}
    assert bets["a"].pi_dominant == pytest.approx(0.5)
    assert bets["a"].y_dominant == 1.0
    # market b closes after the cutoff: unresolved for scoring as of 1000, and the later buy is excluded
    assert bets["b"].y0 is None and bets["b"].dominant == 1 and bets["b"].shares == [0.0, 10.0]


def test_two_sided_bet_and_sign():
    b = Bet("c", "ev:c", "x", False, 0, shares=[10.0, 8.0], cost=[4.0, 4.0], y0=1.0)
    assert b.two_sided and b.dominant == 0 and b.sign == 1
    assert not Bet("c", "ev:c", "x", False, 0, shares=[10.0, 1.0], cost=[4.0, 0.5]).two_sided


def test_skill_stats_positive_for_winner_and_zero_events_when_unresolved():
    conds = {f"c{i}": (1.0 if i % 4 else 0.0, 10) for i in range(40)}  # 30 of 40 outcome-0 wins at entry 0.5
    meta = meta_for(conds)
    rows = [row(1, f"c{i}-0", price=0.5) for i in range(40)]
    s = skill_stats(build_bets(rows, meta))
    assert s.n_resolved == 40 and s.n_events == 40
    assert s.excess == pytest.approx(10.0)
    assert s.z == pytest.approx(10.0 / math.sqrt(40 * 0.25))
    assert s.theta > 0 and s.p_value < 0.01
    empty = skill_stats(build_bets(rows, meta_for({f"c{i}": (None, None) for i in range(40)})))
    assert empty.n_resolved == 0 and empty.p_value == 1.0


def test_correlated_bets_in_one_event_count_once():
    conds = {f"c{i}": (1.0, 10) for i in range(10)}
    meta = meta_for(conds)
    for t in meta.tokens.values():
        object.__setattr__(t, "event_key", "ev:same")
    rows = [row(1, f"c{i}-0", price=0.5) for i in range(10)]
    s = skill_stats(build_bets(rows, meta))
    assert s.n_events == 1
    assert s.z == pytest.approx(5.0 / 5.0)  # excess 5, sd = 10 * 0.5


def test_resolved_condition_pnl_cash_flows():
    meta = meta_for({"a": (1.0, 10), "b": (0.0, 10), "c": (None, None)})
    rows = [
        row(1, "a-0", price=0.4, size=10),  # -4, redeemed for 10 below
        row(2, "a-0", side="SELL", price=0.7, size=2),  # +1.4
        row(3, "a-0", kind="REDEEM", price=0, size=8, usdc=8),
        row(1, "b-0", price=0.3, size=10),  # loses: -3, never redeemed
        row(1, "b-1", price=0.6, size=5),  # wins 5 unredeemed: -3 + 5
        row(1, "c-0", price=0.5, size=10),  # unresolved: excluded
    ]
    pnl = resolved_condition_pnl(rows, meta)
    assert pnl["a"] == pytest.approx(-4 + 1.4 + 8)
    assert pnl["b"] == pytest.approx(-3 - 3 + 5)
    assert "c" not in pnl


def test_merge_counts_as_exit():
    meta = meta_for({"a": (1.0, 10)})
    rows = [
        row(1, "a-0", price=0.45, size=10),
        row(1, "a-1", price=0.5, size=10),
        row(2, "", kind="MERGE", price=0, size=10, usdc=10, condition="a"),
    ]
    assert resolved_condition_pnl(rows, meta)["a"] == pytest.approx(10 - 9.5)


def test_features_concentration_and_grinder():
    conds = {f"c{i}": (1.0, 10) for i in range(5)}
    meta = meta_for(conds)
    rows = [row(1, "c0-0", price=0.1, size=1000)] + [row(1, f"c{i}-0", price=0.97, size=10) for i in range(1, 5)]
    bets = build_bets(rows, meta)
    f = features(rows, meta, bets)
    assert f.top_event_share > 0.9
    assert f.pnl_ex_top3 == pytest.approx(0.6)  # two remaining 0.3 grinder wins
    assert f.median_entry == pytest.approx(0.97)
    assert f.high_price_buy_share == pytest.approx(38.8 / 138.8)


def test_dersimonian_laird_not_swamped_by_noisy_wallets():
    from sniper.analytics import dersimonian_laird_tau2

    rng = np.random.default_rng(3)
    true = rng.normal(0.0, 0.2, 400)
    ses = np.where(np.arange(400) < 300, 3.0, 0.05)  # most wallets have tiny samples
    est = true + rng.normal(0.0, ses)
    naive = max(0.0, float(np.var(est, ddof=1) - np.mean(ses**2)))
    assert naive == 0.0
    assert dersimonian_laird_tau2(est, ses) == pytest.approx(0.04, rel=0.5)
