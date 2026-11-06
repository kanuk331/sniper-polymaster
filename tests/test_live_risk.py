import math

import pytest

from sniper.live.risk import Exposure, RiskConfig, RiskEngine, kelly_fraction_star


def exp(market="m1", event="e1", leader="w1", category="politics") -> Exposure:
    return Exposure(market, event, leader, category)


def test_fixed_stake_rejected_by_market_then_event_limit():
    r = RiskEngine(RiskConfig())  # $10k: market $200, event $500, leader $1.5k, category $3k, total $5k
    assert r.desired_stake(0.4, 0.2, 0.04) == 100
    for _ in range(2):
        d = r.check(100, exp(), 0)
        assert d.approved and d.stake == 100
        r.open(exp(), 100)
    d = r.check(100, exp(), 0)
    assert (d.reason, d.stake) == ("risk_market_limit", 0.0)
    assert d.usage["market"] == pytest.approx(1.0)
    r.open(exp(market="m2"), 200)
    r.open(exp(market="m3"), 100)
    assert r.check(100, exp(market="m4"), 0).reason == "risk_event_limit"
    assert r.check(100, exp(market="m4", event="e2"), 0).approved
    r.release(exp(), 100)
    d = r.check(100, exp(), 0)
    assert d.approved and d.usage["event"] == pytest.approx(1.0)


def test_leader_category_and_total_limits():
    r = RiskEngine(RiskConfig(bankroll=1000, max_market=1, max_event=1))
    r.open(exp(leader="w1", category="sports"), 150)
    assert r.check(1, exp(leader="w1", category="sports"), 0).reason == "risk_leader_limit"
    r.open(exp(leader="w2", category="sports"), 150)
    assert r.check(10, exp(leader="w3", category="sports"), 0).reason == "risk_category_limit"
    r.open(exp(leader="w3", category="crypto"), 150)
    r.open(exp(leader="w4", category="politics"), 50)
    assert r.check(10, exp(leader="w5", category="tech"), 0).reason == "risk_total_limit"
    r.release(exp(leader="w4", category="politics"), 50)
    assert r.total_deployed == pytest.approx(450) and "w4" not in r.deployed["leader"]
    assert r.check(10, exp(leader="w5", category="tech"), 0).approved


def test_kelly_formula_and_clipping():
    assert kelly_fraction_star(0.5, 0.0, 0.0) == 0.0
    assert kelly_fraction_star(0.4, -0.5, 0.0) == 0.0
    assert kelly_fraction_star(1.0, 1.0) == 0.0
    q = 1 / (1 + math.exp(-(math.log(0.4 / 0.6) + 0.5)))
    p_eff = 0.4 + 0.04 * 0.4 * 0.6
    assert kelly_fraction_star(0.4, 0.5, 0.04) == pytest.approx((q - p_eff) / (1 - p_eff))
    r = RiskEngine(RiskConfig(stake_mode="kelly"))
    stake = r.desired_stake(0.4, 0.5, 0.04)
    assert stake == pytest.approx(10_000 * 0.25 * kelly_fraction_star(0.4, 0.5, 0.04)) and stake > 200
    d = r.check(stake, exp(), 0)
    assert d.approved and d.stake == pytest.approx(200)  # clipped to the $200 market limit
    r.open(exp(), 199.5)
    assert r.check(stake, exp(), 0).reason == "risk_market_limit"  # $0.50 left is below min_stake_usdc


def test_daily_and_weekly_loss_stops():
    r = RiskEngine(RiskConfig())  # daily stop -$300, weekly -$700
    day = 20_000 * 86400  # a Friday (UTC); the next Monday is day 20003
    r.realize(-299, day + 10)
    assert r.check(100, exp(), day + 20).approved
    r.realize(-1, day + 30)
    assert r.check(100, exp(), day + 40).reason == "risk_daily_stop"
    assert r.check(100, exp(), day + 86400).approved
    r.realize(-400, day + 86400 + 10)
    assert r.check(100, exp(), day + 2 * 86400).reason == "risk_weekly_stop"
    assert r.check(100, exp(), 20_003 * 86400).approved


def test_kill_switch_pause_resume_and_state():
    r = RiskEngine(RiskConfig())
    assert not r.update_equity(11_000)
    assert not r.update_equity(9_400)  # -14.5% from the peak
    assert r.update_equity(9_350)  # -15%: trips once
    assert r.check(100, exp(), 0).reason == "risk_kill_switch"
    assert not r.update_equity(9_000)
    restored = RiskEngine(RiskConfig())
    restored.restore(r.state())
    assert restored.killed and restored.peak_equity == 11_000
    r.resume()
    assert r.check(100, exp(), 0).approved and r.peak_equity == 9_000
    r.paused = True
    assert r.check(100, exp(), 0).reason == "paused"
