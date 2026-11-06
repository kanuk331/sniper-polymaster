from sniper.db import Database, activity_record, category_from_fee_type, parse_gamma_time


def trade(ts, side="BUY", price=0.4, size=10.0, tx="0xt1", token="t1"):
    return {
        "timestamp": ts,
        "type": "TRADE",
        "side": side,
        "price": price,
        "size": size,
        "transaction_hash": tx,
        "token_id": token,
        "condition_id": "0xc1",
        "outcome_index": 0,
        "slug": "m",
    }


def test_activity_upsert_is_idempotent_and_keeps_identical_fills():
    db = Database(":memory:")
    rows = [
        activity_record("0xW", trade(1)),
        activity_record("0xW", trade(1)),
        activity_record("0xW", trade(2, tx="0xt2")),
    ]
    assert db.upsert_activity(rows) == 3
    again = [
        activity_record("0xW", trade(1)),
        activity_record("0xW", trade(1)),
        activity_record("0xW", trade(2, tx="0xt2")),
    ]
    assert db.upsert_activity(again) == 0
    stored = db.activity("0xw")
    assert len(stored) == 3
    assert stored[0]["usdc_size"] == 4.0


def test_activity_until_filter():
    db = Database(":memory:")
    db.upsert_activity([activity_record("0xw", trade(t, tx=f"0x{t}")) for t in (1, 2, 3)])
    assert [r["ts"] for r in db.activity("0xw", until=3)] == [1, 2]


def test_universe_and_names():
    db = Database(":memory:")
    db.add_universe(
        "s1", [{"address": "0xAA", "source": "lb", "user_name": "alice"}, {"address": "0xbb", "source": "lb"}]
    )
    assert db.universe() == ["0xaa", "0xbb"]
    assert db.wallet_names()["0xaa"] == "alice"


def test_tokens_markets_and_refresh_queries():
    db = Database(":memory:")
    db.upsert_activity([activity_record("0xw", trade(1))])
    assert db.token_ids_needing_refresh() == ["t1"]
    db.upsert_tokens(
        [
            {
                "token_id": "t1",
                "condition_id": "0xc1",
                "outcome_index": 0,
                "resolved": True,
                "final_price": 1.0,
                "closed": True,
                "event_id": 7,
            }
        ]
    )
    assert db.token_ids_needing_refresh() == []
    assert db.condition_ids_needing_market() == {"0xc1": True}
    db.upsert_markets(
        [
            {
                "conditionId": "0xc1",
                "feeType": "sports_fees_v3",
                "feeSchedule": {"rate": 0.05},
                "closedTime": "2026-11-06 14:13:04.014295+00",
                "closed": True,
            }
        ]
    )
    m = db.markets()["0xc1"]
    assert (m["category"], m["fee_rate"]) == ("sports", 0.05)
    assert m["closed_time"] == 1793974384
    assert db.condition_ids_needing_market() == {}


def test_price_windows():
    db = Database(":memory:")
    db.add_prices("t1", 100, 200, [(100, 0.4), (160, 0.45)])
    assert db.has_price_window("t1", 120, 180)
    assert not db.has_price_window("t1", 50, 180)
    assert db.prices("t1", 100, 150) == [(100, 0.4)]


def test_helpers():
    assert category_from_fee_type(None) == "unknown"
    assert category_from_fee_type("crypto_fees_v2") == "crypto"
    assert parse_gamma_time("2026-07-01T00:00:00Z") == 1782864000
    assert parse_gamma_time("garbage") is None


def test_merge_windows_joins_and_caps_length():
    from sniper.ingest import merge_windows

    w = merge_windows([("t", 0), ("t", 100), ("t", 10_000)], pad_after=50, join_gap=60)
    assert w == {"t": [(0, 150), (10_000, 10_050)]}
    capped = merge_windows([("t", i * 1000) for i in range(10)], pad_after=500, join_gap=600, max_len=3000)
    assert all(b - a <= 3000 for a, b in capped["t"]) and len(capped["t"]) > 1


def test_add_prices_many():
    db = Database(":memory:")
    db.add_prices_many([("a", 0, 100, [(10, 0.5)]), ("b", 0, 100, [(20, 0.6), (30, 0.7)])])
    assert db.prices("b", 0, 100) == [(20, 0.6), (30, 0.7)] and db.has_price_window("a", 0, 100)
