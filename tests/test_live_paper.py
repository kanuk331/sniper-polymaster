import asyncio
import json

import pytest

from sniper.live.book import BookSnapshot, Level, OrderBook
from sniper.live.markets import MarketInfo
from sniper.live.paper import PaperConfig, PaperEngine, PaperListener
from sniper.live.report import lots_from_store
from sniper.live.risk import RiskConfig
from sniper.live.signals import LeaderOrder
from sniper.live.store import PaperStore
from sniper.live.watchlist import WatchEntry
from sniper.metrics import capital_and_drawdown, summarize

T = 1_794_000_000.0  # 2026-11: taker fees in force (fees.taker_fee_rate)
ENTRY = WatchEntry("0xaa", tier="candidate", role="alert", user_name="leader", theta_shrunk=0.2)
INFO = MarketInfo("t1", "0xc-t1", category="politics", fee_rate=0.04, event_key="ev:1")


def book(asks=((0.40, 10_000),), bids=((0.38, 10_000),), min_size=5.0, token="t1") -> OrderBook:
    return OrderBook(token, bids=[Level(*b) for b in bids], asks=[Level(*a) for a in asks], min_order_size=min_size)


def snap(b, token="t1", t=T) -> BookSnapshot:
    return BookSnapshot(token, b, int(t * 1000) - 120, int(t * 1000), None if b is not None else "HTTP 404: no book")


def order(side="BUY", size=250.0, price=0.40, tx="0x1", wallet="0xaa", token="t1", detected=T) -> LeaderOrder:
    return LeaderOrder(
        tx_hash=tx,
        wallet=wallet,
        token_id=token,
        side=side,
        condition_id=f"0xc-{token}",
        outcome="Yes",
        title="Will it happen?",
        event_slug="it-happens",
        size=size,
        notional=size * price,
        worst_price=price,
        n_fills=1,
        first_ts=int(detected) - 3,
        last_ts=int(detected) - 3,
        first_received=detected - 1.5,
        detected_at=detected,
    )


class FakeBooks:
    def __init__(self, books=None, t=T):
        self.books = dict(books or {})
        self.t = t
        self.calls: list[str] = []

    async def fetch(self, token_id: str) -> BookSnapshot:
        self.calls.append(token_id)
        return snap(self.books.get(token_id), token_id, self.t)


class FakeMarkets:
    def __init__(self, resolved=None):
        self.resolved = dict(resolved or {})

    async def get(self, token_id: str, condition_id: str = "") -> MarketInfo:
        return MarketInfo(token_id, condition_id, category="politics", fee_rate=0.04, event_key="ev:1")

    async def resolutions(self, token_ids) -> dict[str, float]:
        wanted = set(token_ids)
        return {t: p for t, p in self.resolved.items() if t in wanted}


class Clock:
    def __init__(self, t=T):
        self.t = t

    def __call__(self) -> float:
        return self.t


def engine(store=None, books=None, markets=None, *, listener=None, clock=None, delays=(0,), risk=None, **cfg):
    return PaperEngine(
        store or PaperStore(":memory:"),
        books or FakeBooks(),
        markets or FakeMarkets(),
        PaperConfig(delays=delays, risk=risk or RiskConfig(), **cfg),
        listener=listener,
        clock=clock or Clock(),
    )


def buy(eng, b, o=None, entry=ENTRY, info=INFO):
    o = o or order()
    oid, _ = eng.record_order(o, entry)
    return eng.enter(o, oid, entry, eng.variants[0], snap(b, o.token_id), info)


def test_entry_fills_on_real_asks_and_persists():
    eng = engine()
    out = buy(eng, book(asks=((0.40, 100), (0.41, 1000)), bids=((0.38, 500),)))
    lot = out.lot
    shares = 100 + 60 / 0.41
    fee = 100 * 0.04 * 0.40 * 0.60 + (60 / 0.41) * 0.04 * 0.41 * 0.59  # per level
    assert lot.status == "filled" and (lot.period, lot.strategy, lot.latency_s) == ("live", "baseline", 0)
    assert lot.cost == pytest.approx(100) and lot.shares == pytest.approx(shares)
    assert lot.exec_price == pytest.approx(100 / shares) and lot.ref_price == pytest.approx(0.39)
    assert lot.entry_fee == pytest.approx(fee) and lot.exec_ts == int(T)
    [(ts, dp, dc)] = lot.flows
    assert ts == int(T) and dp == pytest.approx(-fee) and dc == pytest.approx(100)
    assert (lot.signal_ts, lot.leader_price, lot.leader_worst, lot.bench_cost) == (int(T) - 3, 0.40, 0.40, 100)
    assert lot.leader_notional == pytest.approx(100)
    assert (lot.category, lot.event_key, lot.tier, lot.user_name) == ("politics", "ev:1", "candidate", "leader")
    assert lot.slippage_usd == pytest.approx((100 / shares - 0.40) * shares)
    assert out.usage["market"] == pytest.approx(0.5)  # $100 of the $200 market limit
    assert out.uncapped is not None and out.uncapped.complete and out.cap == pytest.approx(0.415)
    [row] = eng.store.rows("SELECT * FROM lots")
    assert row["status"] == "filled" and row["open_shares"] == pytest.approx(shares)
    assert row["bench_shares"] == pytest.approx(250) and row["variant"] == "baseline@0s"
    assert json.loads(row["diag_json"])["usdc_under_cap"] == pytest.approx(0.40 * 100 + 0.41 * 1000)
    [s] = eng.store.rows("SELECT * FROM snapshots")
    assert (s["purpose"], s["best_ask"], s["best_bid"], s["vwap_complete"]) == ("entry", 0.40, 0.38, 1)
    assert s["ask_depth_usdc"] == pytest.approx(40 + 410) and json.loads(s["levels_json"])["asks"][0] == [0.40, 100]
    assert eng.risk[eng.variants[0]].total_deployed == pytest.approx(100)
    assert eng.marks["t1"] == pytest.approx(0.39)


@pytest.mark.parametrize(
    ("b", "o", "expected"),
    [
        (book(asks=((0.42, 10_000),)), order(), "skip:premium"),  # best ask above 0.40 + 0.015
        (book(asks=((0.41, 100), (0.43, 10_000))), order(), "skip:depth"),  # only $41 at or below the cap
        (book(asks=((0.98, 10_000),)), order(price=0.975), "skip:extreme_price"),
        (book(asks=((0.02, 100_000),)), order(price=0.02), "skip:extreme_price"),
        (book(min_size=1000), order(), "skip:min_size"),  # 250 shares < 1000
        (None, order(), "skip:no_book"),
    ],
)
def test_entry_skip_reasons(b, o, expected):
    eng = engine()
    lot = buy(eng, b, o).lot
    assert lot.status == expected
    assert (lot.shares, lot.cost, lot.entry_fee, lot.flows, lot.exec_price) == (0.0, 0.0, 0.0, [], None)
    assert lot.bench_cost == 100 and eng.risk[eng.variants[0]].total_deployed == 0
    s = summarize([lot], n_boot=10)
    assert (s["filled"], s["missed"], s["missed_by_reason"]) == (0, 1, {expected.removeprefix("skip:"): 1})


def test_price_cap_is_inclusive_and_partial_fills_are_opt_in():
    assert buy(engine(), book(asks=((0.415, 10_000),))).lot.status == "filled"
    lot = buy(engine(min_fill_ratio=0.4), book(asks=((0.41, 100), (0.43, 10_000)))).lot
    assert lot.status == "filled" and lot.cost == pytest.approx(41) and lot.bench_cost == pytest.approx(41)


def test_risk_limits_and_manual_pause_become_skips():
    eng = engine(risk=RiskConfig(max_market=0.015))  # $150 per market
    assert buy(eng, book()).lot.status == "filled"
    assert buy(eng, book(), order(tx="0x2")).lot.status == "skip:risk_market_limit"
    eng.pause()
    assert eng.paused and buy(eng, book(token="t2"), order(tx="0x3", token="t2")).lot.status == "skip:paused"
    eng.resume()
    assert buy(eng, book(token="t2"), order(tx="0x4", token="t2")).lot.status == "filled"


def test_kelly_mode_sizes_from_edge_and_skips_without_edge():
    eng = engine(risk=RiskConfig(stake_mode="kelly"))
    lot = buy(eng, book(), entry=WatchEntry("0xaa", "candidate", theta_shrunk=0.5)).lot
    assert lot.status == "filled" and lot.cost == pytest.approx(200)  # Kelly ~$480, clipped to the market limit
    no_edge = WatchEntry("0xaa", "candidate", theta_shrunk=-0.5)
    lot = buy(eng, book(token="t2"), order(tx="0x2", token="t2"), entry=no_edge).lot
    assert lot.status == "skip:no_edge" and lot.bench_cost == 100


def test_proportional_exit_then_settlement_matches_metrics():
    eng = engine()
    v = eng.variants[0]
    eng.set_holdings("0xaa", {"t1": 750.0})
    lot = buy(eng, book()).lot  # leader 750 -> 1000 shares; follower 250 shares for $100
    entry_fee = 250 * 0.04 * 0.40 * 0.60
    oid, frac = eng.record_order(order(side="SELL", size=250, price=0.50, tx="0x2"), ENTRY)
    assert frac == pytest.approx(0.25) and eng.holdings["0xaa"]["t1"] == pytest.approx(750)

    ex = eng.exit(v, "0xaa", "t1", frac, snap(book(bids=((0.48, 10_000),))), bench_price=0.50, order_id=oid)
    exit_fee = 62.5 * 0.04 * 0.48 * 0.52
    assert (ex.status, ex.lots) == ("ok", 1)
    assert ex.shares == pytest.approx(62.5) and ex.proceeds == pytest.approx(30) and ex.fee == pytest.approx(exit_fee)
    assert ex.pnl == pytest.approx(30 - exit_fee - 25)
    assert lot.exit_proceeds == pytest.approx(30) and lot.bench_value == pytest.approx(62.5 * 0.50)
    _, dp, dc = lot.flows[1]
    assert dp == pytest.approx(30 - exit_fee - 25) and dc == pytest.approx(-25)
    assert eng.risk[v].total_deployed == pytest.approx(75)

    closed = eng.settle("t1", 1.0, ts=T + 3600)
    assert len(closed) == 1 and closed[0].value == pytest.approx(187.5)
    assert lot.settle_value == pytest.approx(187.5) and lot.close_ts == int(T) + 3600
    assert lot.bench_value == pytest.approx(31.25 + 187.5)
    pnl = 30 + 187.5 - 100 - entry_fee - exit_fee
    assert lot.pnl == pytest.approx(pnl) and sum(dp for _, dp, _ in lot.flows) == pytest.approx(pnl)
    assert eng.positions[v] == {} and eng.risk[v].total_deployed == pytest.approx(0)
    assert eng.risk[v].realized == pytest.approx(pnl)

    [stored] = lots_from_store(eng.store)
    s = summarize([stored], n_boot=10)
    assert s["pnl"] == pytest.approx(pnl) and s["cost"] == pytest.approx(100) and s["roi"] == pytest.approx(pnl / 100)
    assert s["fees"] == pytest.approx(entry_fee + exit_fee) and s["leader_roi"] == pytest.approx(1.1875)
    assert s["open_share"] == 0 and s["win_rate"] == 1.0
    assert capital_and_drawdown([stored])["peak_capital"] == pytest.approx(100)
    kinds = [(r["kind"], r["status"]) for r in eng.store.rows("SELECT kind, status FROM exits ORDER BY id")]
    assert kinds == [("sell", "ok"), ("settle", "ok")]
    assert eng.store.settlements() == {"t1": 1.0}


def test_exit_with_thin_or_missing_bids_keeps_the_rest():
    eng = engine()
    v = eng.variants[0]
    buy(eng, book())  # 250 shares, $100
    ex = eng.exit(v, "0xaa", "t1", 1.0, snap(book(bids=((0.45, 100),))), bench_price=0.5)
    assert ex.status == "partial" and ex.shares == pytest.approx(100)
    [ol] = eng.positions[v][("0xaa", "t1")]
    assert ol.shares == pytest.approx(150) and ol.basis == pytest.approx(60) and ol.bench_shares == 0
    assert eng.exit(v, "0xaa", "t1", 1.0, snap(book(bids=())), bench_price=0.5).status == "no_bids"
    assert eng.exit(v, "0xaa", "t1", 1.0, snap(None), bench_price=0.5).status == "no_book"
    assert ol.shares == pytest.approx(150)
    statuses = [r["status"] for r in eng.store.rows("SELECT status FROM exits ORDER BY id")]
    assert statuses == ["partial", "no_bids", "no_book"]


def test_exit_only_reduces_lots_detected_before_it():
    eng = engine()
    v = eng.variants[0]
    for tx, seq in (("0x1", 1), ("0x2", 3)):
        o = order(tx=tx)
        oid, _ = eng.record_order(o, ENTRY)
        eng.enter(o, oid, ENTRY, v, snap(book()), INFO, seq=seq)
    eng.exit(v, "0xaa", "t1", 1.0, snap(book(bids=((0.5, 10_000),))), bench_price=0.5, before_seq=2)
    [left] = eng.positions[v][("0xaa", "t1")]
    assert left.seq == 3 and left.shares == pytest.approx(250) and left.bench_shares == pytest.approx(250)


def test_late_fills_update_holdings_without_a_new_signal():
    eng = engine()
    eng.record_order(order(), ENTRY)
    assert eng.record_order(order(size=10), ENTRY)[0] is None
    assert eng.holdings["0xaa"]["t1"] == pytest.approx(260)
    [row] = eng.store.rows("SELECT size, late_fills FROM orders")
    assert row["size"] == pytest.approx(260) and row["late_fills"] == 1


def test_on_order_schedules_entries_and_mirrored_exits_per_variant():
    books = FakeBooks({"t1": book(asks=((0.40, 10_000),), bids=((0.45, 10_000),))})

    async def run():
        eng = engine(books=books, delays=(0, 5))
        eng.set_holdings("0xaa", {"t1": 250.0})
        eng.on_order(order(detected=T - 100), ENTRY)  # leader 250 -> 500 shares
        eng.on_order(order(side="SELL", size=250, price=0.45, tx="0x2", detected=T - 50), ENTRY)  # sells half
        eng.on_order(order(side="SELL", size=5, tx="0x3", token="t9", detected=T - 40), ENTRY)  # nothing copied
        assert await eng.drain(5) == 0
        return eng

    eng = asyncio.run(run())
    assert [v.delay_s for v in eng.variants] == [0, 5]
    for v in eng.variants:
        [ol] = eng.positions[v][("0xaa", "t1")]
        assert ol.shares == pytest.approx(125) and ol.lot.exit_proceeds == pytest.approx(125 * 0.45)
        assert ol.bench_shares == pytest.approx(125) and ol.lot.bench_value == pytest.approx(125 * 0.45)
    assert (eng.counters["buy_orders"], eng.counters["sell_orders"]) == (1, 2)
    assert books.calls == ["t1"] * 4  # entry + exit per variant, nothing for the uncopied token
    purposes = [r["purpose"] for r in eng.store.rows("SELECT purpose FROM snapshots")]
    assert sorted(purposes) == ["entry", "entry", "sell", "sell"]


def test_restart_restores_open_positions_and_risk_state(tmp_path):
    path = tmp_path / "paper.sqlite"
    eng = engine(store=PaperStore(path))
    v = eng.variants[0]
    buy(eng, book())
    eng.exit(v, "0xaa", "t1", 0.25, snap(book(bids=((0.48, 10_000),))), bench_price=0.5)
    realized = eng.risk[v].realized
    eng.pause()
    eng.store.close()

    eng2 = engine(store=PaperStore(path))
    assert eng2.load() == 1
    v2 = eng2.variants[0]
    [ol] = eng2.positions[v2][("0xaa", "t1")]
    assert ol.shares == pytest.approx(187.5) and ol.basis == pytest.approx(75)
    assert ol.bench_shares == pytest.approx(187.5) and ol.fee_rate == 0.04 and ol.seq == 0
    assert eng2.risk[v2].total_deployed == pytest.approx(75) and eng2.risk[v2].realized == pytest.approx(realized)
    assert eng2.risk[v2].paused and eng2.titles["t1"] == "Will it happen? · Yes"
    _, frac = eng2.record_order(order(side="SELL", size=10, tx="0x9"), ENTRY)
    assert frac == 1.0  # holdings unknown after the restart: mirror as a full exit


def test_reconcile_mirrors_quiet_reductions_as_merge_exits():
    clock = Clock()
    books = FakeBooks({"t1": book(bids=((0.50, 10_000),), asks=((0.52, 10_000),))})

    async def run():
        eng = engine(books=books, clock=clock)
        eng.set_holdings("0xaa", {"t1": 750.0})
        buy(eng, book())  # leader 1000 shares, feed activity at T
        clock.t = T + 10
        assert eng.reconcile("0xaa", {"t1": 500.0, "t9": 40.0}) == []  # t1 traded recently: not quiet yet
        assert eng.holdings["0xaa"] == {"t1": 1000.0, "t9": 40.0}
        clock.t = T + 700
        reductions = eng.reconcile("0xaa", {"t1": 500.0, "t9": 40.0})
        assert reductions == [("t1", pytest.approx(0.5))] and eng.holdings["0xaa"]["t1"] == 500.0
        assert await eng.apply_reductions("0xaa", reductions) == 1
        await eng.drain(5)
        return eng

    eng = asyncio.run(run())
    [ol] = eng.positions[eng.variants[0]][("0xaa", "t1")]
    assert ol.shares == pytest.approx(125) and ol.lot.exit_proceeds == pytest.approx(125 * 0.50)
    assert ol.lot.bench_value == pytest.approx(125 * 0.51)  # benchmark leg valued at the mid
    assert [r["kind"] for r in eng.store.rows("SELECT kind FROM exits")] == ["merge"]


def test_reduction_on_a_resolved_market_settles_instead():
    async def run():
        eng = engine(markets=FakeMarkets(resolved={"t1": 0.0}))
        buy(eng, book())
        return eng, await eng.apply_reductions("0xaa", [("t1", 1.0)])

    eng, scheduled = asyncio.run(run())
    assert scheduled == 0 and eng.positions[eng.variants[0]] == {}
    [lot] = lots_from_store(eng.store)
    assert lot.settle_value == 0 and lot.pnl == pytest.approx(-100 - 250 * 0.04 * 0.24)


class Recorder(PaperListener):
    def __init__(self):
        self.events: list[tuple] = []

    async def settled(self, token_id, final_price, closed):
        self.events.append(("settled", token_id, final_price, len(closed)))

    async def kill_switch(self, variant, equity, peak):
        self.events.append(("kill", variant.name, round(equity, 2), peak))


def test_mark_to_market_trips_kill_switch_then_resolution_settles():
    rec = Recorder()
    books = FakeBooks({"t1": book(asks=((0.04, 10_000),), bids=((0.02, 10_000),))})
    limits = dict(max_market=1, max_event=1, max_leader=1, max_category=1, max_total=1)
    risk = RiskConfig(bankroll=1000, kill_switch_drawdown=0.05, **limits)

    async def run():
        eng = engine(books=books, markets=FakeMarkets(resolved={"t1": 1.0}), listener=rec, risk=risk)
        buy(eng, book())  # $100 at 0.40, fee $2.40
        rows = await eng.mark_to_market()
        skipped = buy(eng, book(token="t2"), order(tx="0x2", token="t2")).lot
        settled = await eng.check_resolutions()
        return eng, rows, skipped, settled

    eng, rows, skipped, settled = asyncio.run(run())
    [row] = rows
    equity = 1000 - 2.4 + (250 * 0.03 - 100)
    assert row["unrealized"] == pytest.approx(250 * 0.03 - 100) and row["equity"] == pytest.approx(equity)
    assert (row["killed"], row["open_lots"], row["unmarked"]) == (1, 1, 0)
    assert rec.events[0] == ("kill", "baseline@0s", round(equity, 2), 1000)
    assert skipped.status == "skip:risk_kill_switch"
    assert settled == 1 and rec.events[1] == ("settled", "t1", 1.0, 1)
    assert eng.risk[eng.variants[0]].realized == pytest.approx(-2.4 + 150)
    assert [r["purpose"] for r in eng.store.rows("SELECT purpose FROM snapshots WHERE purpose = 'mtm'")] == ["mtm"]
    assert len(eng.store.rows("SELECT * FROM equity")) == 1
