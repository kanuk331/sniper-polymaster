import json
import sqlite3

import pytest

from sniper.live import cli
from sniper.live.book import BookSnapshot, Level, OrderBook
from sniper.live.markets import MarketInfo
from sniper.live.paper import PaperConfig, PaperEngine
from sniper.live.report import build_report, format_report, lots_from_store, mtm_drawdown
from sniper.live.signals import LeaderOrder
from sniper.live.store import PaperStore
from sniper.live.watchlist import WatchEntry

T = 1_794_000_000.0


class NoBooks:
    async def fetch(self, token_id):
        raise AssertionError("not used")


def snap(asks, token):
    b = OrderBook(token, bids=[Level(0.38, 10_000)], asks=[Level(*a) for a in asks])
    return BookSnapshot(token, b, int(T * 1000) - 200, int(T * 1000))


def order(wallet, token, tx):
    return LeaderOrder(
        tx_hash=tx, wallet=wallet, token_id=token, side="BUY", condition_id=f"0xc-{token}", outcome="Yes",
        title=f"Market {token}", size=250, notional=100, worst_price=0.40, n_fills=1, first_ts=int(T) - 3,
        first_received=T - 2, detected_at=T - 1,
    )  # fmt: skip


def populate(store: PaperStore) -> PaperEngine:
    eng = PaperEngine(store, NoBooks(), None, PaperConfig(delays=(0, 60)), clock=lambda: T)
    signals = [
        ("0xaa", "lead", "t1", "politics", "0x1"),
        ("0xbb", "watcher", "t2", "sports", "0x2"),
    ]
    for wallet, name, token, category, tx in signals:
        entry = WatchEntry(wallet, "candidate" if wallet == "0xaa" else "watch", user_name=name)
        info = MarketInfo(token, f"0xc-{token}", category=category, fee_rate=0.04, event_key=f"ev:{token}")
        o = order(wallet, token, tx)
        oid, _ = eng.record_order(o, entry)
        for v in eng.variants:
            asks = ((0.40, 10_000),) if v.delay_s == 0 else ((0.45, 10_000),)  # 60 s: the price ran away
            eng.enter(o, oid, entry, v, snap(asks, token), info, scheduled=o.detected_at + v.delay_s)
    eng.settle("t1", 1.0, ts=T + 86400)
    for ts, equity in ((T, 10_000), (T + 1, 10_100), (T + 2, 9_900), (T + 3, 10_050)):
        store.insert_equity({"ts": int(ts), "variant": "baseline@0s", "equity": equity, "unmarked": 0})
    return eng


def test_report_breakdowns_mtm_and_execution(tmp_path):
    store = PaperStore(tmp_path / "paper.sqlite")
    populate(store)
    rep = build_report(store, n_boot=50)
    assert rep["lots"] == 4
    by_lat = {r["latency_s"]: r for r in rep["by_latency"]}
    assert (by_lat[0]["signals"], by_lat[0]["filled"]) == (2, 2)
    assert (by_lat[60]["filled"], by_lat[60]["missed_by_reason"]) == (0, {"premium": 2})
    fee = 250 * 0.04 * 0.4 * 0.6
    assert by_lat[0]["pnl"] == pytest.approx(250 - 100 - fee - fee)  # t1 won, t2 still open at cost
    assert by_lat[0]["open_share"] == pytest.approx(0.5)
    assert by_lat[60]["missed_leader_pnl"] == pytest.approx(150)  # the leader's won t1 trade the 60 s copy missed
    assert {(r["category"], r["latency_s"]) for r in rep["by_category_latency"]} == {
        ("politics", 0), ("politics", 60), ("sports", 0), ("sports", 60)
    }  # fmt: skip
    assert {(r["user_name"], r["tier"]) for r in rep["by_wallet_latency"]} == {
        ("lead", "candidate"),
        ("watcher", "watch"),
    }
    assert {r["variant"] for r in rep["by_strategy_latency"]} == {"baseline@0s", "baseline@60s"}
    assert rep["mtm"]["baseline@0s"]["max_drawdown"] == pytest.approx(200)
    assert rep["mtm"]["baseline@0s"]["max_drawdown_pct"] == pytest.approx(200 / 10_100)
    ex = rep["execution"]
    assert ex["variants"]["baseline@0s"]["snapshots"] == 2
    assert ex["variants"]["baseline@60s"]["effective_delay_s"]["p50"] == pytest.approx(3.0)
    assert ex["variants"]["baseline@0s"]["fetch_ms"]["p50"] == pytest.approx(200)
    assert ex["decisions"]["baseline@60s"] == {"skip:premium": 2}
    assert ex["variants"]["baseline@60s"]["premium_cents"]["p50"] == pytest.approx(5.0)  # 0.45 vs leader 0.40
    assert ex["variants"]["baseline@0s"]["premium_cents"]["mean"] == pytest.approx(0.0)
    text = format_report(rep)
    assert "== by latency ==" in text and "baseline@60s" in text and "premium:2" in text
    json.dumps(rep, default=str)


def test_lots_from_store_carries_open_positions_at_cost(tmp_path):
    store = PaperStore(tmp_path / "paper.sqlite")
    populate(store)
    lots = {(lot.token_id, lot.latency_s): lot for lot in lots_from_store(store)}
    open_lot = lots[("t2", 0)]
    assert open_lot.open_cost == pytest.approx(100) and open_lot.bench_value == pytest.approx(100)
    assert open_lot.pnl == pytest.approx(-250 * 0.04 * 0.4 * 0.6)  # only the entry fee is realized
    assert lots[("t2", 60)].open_cost == 0 and lots[("t2", 60)].bench_value == pytest.approx(100)


def test_mtm_drawdown_from_rows():
    rows = [{"ts": t, "variant": "v", "equity": e, "unmarked": 0} for t, e in ((1, 100), (2, 80), (3, 120), (4, 90))]
    m = mtm_drawdown(rows)["v"]
    assert (m["points"], m["peak_equity"], m["max_drawdown"]) == (4, 120, 30)


def test_cli_report_writes_json(tmp_path, capsys):
    path = tmp_path / "paper.sqlite"
    populate(PaperStore(path)).store.close()
    out = tmp_path / "rep.json"
    cli.main(["live", "report", "--paper-db", str(path), "--n-boot", "20", "--out", str(out)])
    assert "== by wallet x latency ==" in capsys.readouterr().out
    assert json.loads(out.read_text())["lots"] == 4


def test_store_refuses_a_research_database(tmp_path):
    path = tmp_path / "research.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE activity (x)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="research database"):
        PaperStore(path)
    assert {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")} == {"activity"}
