import pytest

from sniper.backtest import CopyParams, PriceBook
from sniper.copyability import Copyability, FastFilter, assess, copyability_flags, plan_sample, quick_exit_share
from tests.test_backtest import T0, meta, trade


def test_quick_exit_share():
    rows = [
        trade(0, size=100, tx="0x1"),
        trade(300, side="SELL", size=60, tx="0x2"),  # halved within 10 min
        trade(1000, size=100, tx="0x3"),
        trade(5000, side="SELL", size=100, tx="0x4"),
    ]  # slow exit
    signals = [(T0, "a-0"), (T0 + 1000, "a-0")]
    assert quick_exit_share(rows, signals, window_s=600) == pytest.approx(0.5)


def test_flags_thresholds():
    ff = FastFilter()
    ok = Copyability("w", n_signals=100, n_priced=90, n_closed=60, runaway_share={60: 0.1}, copy_roi={60: 0.05})
    assert copyability_flags(ok, ff) == []
    fast = Copyability("w", n_signals=100, n_priced=90, n_closed=60, runaway_share={60: 0.35}, copy_roi={60: 0.05})
    assert copyability_flags(fast, ff) == ["fast_runaway"]
    flip = Copyability(
        "w",
        n_signals=100,
        n_priced=90,
        n_closed=60,
        runaway_share={60: 0.1},
        copy_roi={60: 0.05},
        quick_exit_share=0.25,
    )
    assert copyability_flags(flip, ff) == ["fast_roundtrip"]
    losing = Copyability("w", n_signals=100, n_priced=90, n_closed=60, runaway_share={60: 0.1}, copy_roi={60: -0.01})
    assert copyability_flags(losing, ff) == ["uncopyable_edge"]
    thin = Copyability("w", n_signals=10, n_priced=10, n_closed=5, runaway_share={60: 0.5}, copy_roi={60: 0.5})
    assert copyability_flags(thin, ff) == ["copy_unverified"]


def test_plan_and_assess_use_only_resolved_pre_cutoff_signals():
    as_of = T0 + 100_000
    m = meta(1.0, fee=0.0, closed=T0 + 50_000)  # resolved before the cutoff
    rows = [trade(1000 * i, price=0.40, size=500, tx=f"0x{i}") for i in range(30)]
    strategy = CopyParams(max_premium=0.015, half_spread=0.01)
    ff = FastFilter(sample_signals=25, min_priced=20, min_closed=20)
    sample = plan_sample(rows, m, as_of=as_of, start=T0, strategy=strategy, ff=ff)
    assert len(sample.signals) == 25 and sample.signals[0][0] == T0 + 5000
    # price runs away to 0.43 one minute after every signal: at 60 s everything is skipped
    series = {"a-0": [(T0 + 1000 * i + d, p) for i in range(30) for d, p in ((15, 0.40), (65, 0.43))]}
    c = assess(rows, m, PriceBook(series), sample, wallet="w", as_of=as_of, strategy=strategy, ff=ff)
    assert c.runaway_share[10] == 0.0 and c.runaway_share[60] == 1.0
    assert c.copy_roi[10] == pytest.approx(1 / 0.41 - 1)
    assert "fast_runaway" in c.flags
    # with the cutoff before resolution there is nothing to sample
    early = plan_sample(rows, m, as_of=T0 + 45_000, start=T0, strategy=strategy, ff=ff)
    assert early.signals == []
