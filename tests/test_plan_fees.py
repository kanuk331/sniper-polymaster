import pytest

from sniper.db import Database, activity_record, merge_intervals, subtract_intervals
from sniper.fees import FEE_V2_START, SPORTS_HIKE, taker_fee_rate
from sniper.plan import Period, load_plan, select


def test_intervals():
    assert merge_intervals([(5, 8), (1, 3), (2, 4), (8, 9)]) == [(1, 4), (5, 9)]
    assert subtract_intervals((0, 10), [(2, 4), (6, 12)]) == [(0, 2), (4, 6)]
    assert subtract_intervals((0, 10), []) == [(0, 10)]
    assert subtract_intervals((0, 10), [(-5, 20)]) == []


def test_coverage_and_window_status():
    db = Database(":memory:")
    db.add_coverage("0xW", 100, 200)
    db.add_coverage("0xw", 150, 300)
    assert db.coverage("0xw") == [(100, 300)]
    assert db.window_status("0xw", 120, 280) == "complete"
    assert db.window_status("0xw", 50, 280) == "missing"
    db.add_capped("0xw", 50, 280, low_ts=99)
    assert db.window_status("0xw", 50, 280) == "capped"


def test_migrate_sync_state_uses_earliest_row_for_truncated_wallets():
    db = Database(":memory:")
    row = {"timestamp": 500, "type": "TRADE", "side": "BUY", "price": 0.5, "size": 1, "transaction_hash": "0x1"}
    db.upsert_activity([activity_record("0xa", row, slim=True), activity_record("0xb", row)])
    db.set_sync_state("0xa", 500, 1, truncated=True)
    db.set_sync_state("0xb", 500, 1, truncated=False)
    assert db.migrate_sync_state(since=100) == 2
    synced = db.sync_state("0xa").synced_at
    assert db.coverage("0xa") == [(500, synced)]
    assert db.coverage("0xb") == [(100, synced)]
    assert db.migrate_sync_state(since=100) == 0
    assert db.activity("0xa")[0]["title"] is None


def test_fee_regime():
    assert taker_fee_rate("politics", 0.04, FEE_V2_START - 1) == 0.0
    assert taker_fee_rate("politics", 0.04, FEE_V2_START) == 0.04
    assert taker_fee_rate("sports", 0.05, SPORTS_HIKE - 1) == 0.03
    assert taker_fee_rate("sports", 0.05, SPORTS_HIKE) == 0.05
    assert taker_fee_rate("crypto", 0.07, FEE_V2_START - 86400) == 0.07
    assert taker_fee_rate("crypto", 0.07, 1700000000) == 0.0
    assert taker_fee_rate("geopolitics", 0.0, SPORTS_HIKE) == 0.0


def test_plan_loads_and_windows_are_independent(tmp_path):
    periods = load_plan("config/walkforward.toml")
    assert {p.role for p in periods} == {"dev", "holdout"}
    assert [p.name for p in select(periods, role="holdout")] == ["p2025-02", "p2025-08", "p2026-02", "p2026-07"]
    p = next(x for x in periods if x.name == "p2026-09")
    assert p.as_of == p.split_ts - 7 * 86400 and p.window[1] == p.split_ts + 45 * 86400
    assert p.universe_end_dates == ("2026-03-05T00:00:00Z", "2026-08-25T00:00:00Z")
    bad = tmp_path / "bad.toml"
    bad.write_text(
        '[[period]]\nname="a"\nsplit="2026-01-01"\nrole="dev"\n[[period]]\nname="b"\nsplit="2026-01-20"\nrole="dev"\n'
    )
    with pytest.raises(ValueError, match="overlap"):
        load_plan(bad)
    assert Period("x", "2026-01-01", "dev").snapshot == "wf-x"


def test_wallet_status_cap_is_order_independent():
    from sniper.walkforward import wallet_status

    db = Database(":memory:")
    rows = [
        {"timestamp": 100 + i, "type": "TRADE", "side": "BUY", "price": 0.5, "size": 1, "transaction_hash": f"0x{i}"}
        for i in range(5)
    ]
    db.upsert_activity([activity_record("0xw", r, slim=True) for r in rows])
    db.add_coverage("0xw", 0, 1000)  # complete, e.g. filled in by another period's backfill
    assert wallet_status(db, "0xw", 0, 1000, cap=10) == "complete"
    assert wallet_status(db, "0xw", 0, 1000, cap=5) == "capped"
