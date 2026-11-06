from sniper.analytics import Features, SkillStats
from sniper.db import Database, activity_record
from sniper.scoring import Thresholds, WalletScore, archetype_flags, assign_tier, score_wallets


def feats(**kw) -> Features:
    base = dict(
        n_trades=500,
        active_days=100,
        history_days=200,
        trades_per_active_day=5,
        short_crypto_share=0.0,
        two_sided_share=0.0,
        merge_conversion_share=0.0,
        income_usdc=0.0,
        income_share=0.0,
        high_price_buy_share=0.1,
        median_entry=0.5,
        sell_share=0.1,
        resolved_pnl=1000.0,
        resolved_cost=10000.0,
        roi=0.1,
        top_event_share=0.1,
        pnl_ex_top3=500.0,
        n_events_resolved=100,
        category_shares={"politics": 1.0},
    )
    return Features(**(base | kw))


def stats() -> SkillStats:
    return SkillStats(100, 100, 100, 10.0, 3.0, 0.001, 2.5, 0.3, 0.1, 0.6, 0.5, 50.0)


def test_flags():
    t = Thresholds()
    assert archetype_flags(feats(), t) == []
    assert "mm_or_arb" in archetype_flags(feats(two_sided_share=0.5), t)
    assert "mm_or_arb" in archetype_flags(feats(income_share=0.4), t)
    assert "near_certainty_grinder" in archetype_flags(feats(median_entry=0.96), t)
    assert "concentrated" in archetype_flags(feats(pnl_ex_top3=-1.0), t)
    assert "hft_bot" in archetype_flags(feats(trades_per_active_day=500), t)
    assert "thin_sample" in archetype_flags(feats(n_events_resolved=10), t)


def test_tiers():
    t = Thresholds()
    s = WalletScore("w", None, feats(), stats(), [], q_bh=0.01, theta_shrunk=0.2, theta_q20=0.05)
    assert assign_tier(s, t) == "candidate"
    s.theta_q20 = -0.01
    assert assign_tier(s, t) == "watch"
    s.q_bh = 0.5
    assert assign_tier(s, t) == "noise"
    s.flags = ["thin_sample"]
    assert assign_tier(s, t) == "insufficient"
    s.flags = ["thin_sample", "mm_or_arb"]
    assert assign_tier(s, t) == "excluded"


def test_score_wallets_end_to_end_on_sqlite():
    db = Database(":memory:")
    tokens, markets, rows = [], [], []
    for i in range(60):
        cid = f"0x{i:064x}"
        win0 = 1.0 if i % 3 else 0.0  # wallet buys outcome 0 at 0.5 and wins 2/3 of the time
        for o in (0, 1):
            tokens.append(
                {
                    "token_id": f"t{i}-{o}",
                    "condition_id": cid,
                    "outcome_index": o,
                    "resolved": True,
                    "final_price": win0 if o == 0 else 1 - win0,
                    "closed": True,
                    "event_id": i,
                }
            )
        markets.append(
            {
                "conditionId": cid,
                "feeType": "politics_fees_v2",
                "feeSchedule": {"rate": 0.04},
                "closedTime": "2026-06-01T00:00:00Z",
                "closed": True,
            }
        )
        rows.append(
            activity_record(
                "0xskill",
                {
                    "timestamp": 1767225600 + i * 86400 * 3,
                    "type": "TRADE",
                    "side": "BUY",
                    "price": 0.5,
                    "size": 100,
                    "transaction_hash": f"0x{i}",
                    "token_id": f"t{i}-0",
                    "condition_id": cid,
                    "outcome_index": 0,
                },
            )
        )
    db.upsert_tokens(tokens)
    db.upsert_markets(markets)
    db.upsert_activity(rows)
    db.add_universe("s", [{"address": "0xskill", "source": "test"}])
    [s] = score_wallets(db, ["0xskill"])
    assert s.skill.n_resolved == 60
    assert s.skill.z > 2.5
    assert s.features.resolved_pnl == 60 * 100 * (2 / 3) - 60 * 50
    assert "thin_sample" not in s.flags
