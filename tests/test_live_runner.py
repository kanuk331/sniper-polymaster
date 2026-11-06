import asyncio
import csv
import json
import time

import httpx

from sniper.config import Settings
from sniper.http import ApiClient
from sniper.live.book import BookSnapshot, Level, OrderBook
from sniper.live.paper import PaperConfig
from sniper.live.rtds import RtdsStats, TradeEvent
from sniper.live.runner import LiveRunner, RunConfig
from sniper.live.store import PaperStore
from sniper.live.telegram import DryRunNotifier
from sniper.polymarket import DataApi, GammaApi

FIELDS = ["tier", "wallet", "user_name", "theta_shrunk", "theta_q20", "top_category"]
SCORES = [
    ["candidate", "0xaa", "lead", "0.21", "0.16", "politics"],
    ["watch", "0xbb", "watcher", "0.05", "-0.01", "sports"],
    ["noise", "0xcc", "noisy", "0.0", "-0.2", "sports"],
    ["excluded", "0xdd", "bot", "0.3", "0.2", "crypto"],
]


def write_scores(path):
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELDS)
        writer.writerows(SCORES)


def api_handler(request: httpx.Request) -> httpx.Response:
    path, params = request.url.path, request.url.params
    if path.endswith("/positions"):
        data = [{"token_id": "t1", "current_size": 100.0}] if params["user"] == "0xaa" else []
        return httpx.Response(200, json={"data": data, "pagination": {"has_more": False}})
    if path.endswith("/tokens"):
        row = {"token_id": "t1", "condition_id": "0xc1", "event_id": 7, "neg_risk": False, "resolved": False}
        return httpx.Response(200, json={"data": [row]})
    if path.endswith("/markets"):
        market = {
            "conditionId": "0xc1", "feeType": "politics_fees", "feeSchedule": {"rate": 0.04}, "question": "Q?",
            "slug": "q", "negRisk": False, "events": [{"id": "7", "slug": "ev7"}],
        }  # fmt: skip
        return httpx.Response(200, json=[market])
    return httpx.Response(404, json={})


def api(cls, base):
    http = httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(api_handler))
    return cls(ApiClient(base, general_limit=1000, client=http, max_retries=0))


class FakeRtds:
    def __init__(self, events):
        self._events = events
        self.stats = RtdsStats()

    def stop(self):
        pass

    async def events(self):
        for ev in self._events:
            self.stats.frames += 1
            self.stats.trades += 1
            yield ev
        await asyncio.sleep(3600)


class FakeBooks:
    async def fetch(self, token_id):
        b = OrderBook(token_id, bids=[Level(0.38, 10_000)], asks=[Level(0.40, 10_000)], min_order_size=5)
        now = int(time.time() * 1000)
        return BookSnapshot(token_id, b, now - 50, now)


def trade(wallet, tx, size, now, name):
    return TradeEvent(
        wallet=wallet, side="BUY", size=size, price=0.40, token_id="t1", condition_id="0xc1", outcome="Yes",
        outcome_index=0, tx_hash=tx, ts=int(now) - 1, title="Will <it> happen?", event_slug="ev7", name=name,
        received_at=now,
    )  # fmt: skip


def make_runner(tmp_path, events, **cfg_kw):
    write_scores(tmp_path / "scores.csv")
    cfg = RunConfig(
        scores=tmp_path / "scores.csv",
        paper=PaperConfig(delays=(0,)),
        paper_db=tmp_path / "paper.sqlite",
        alerts_log=tmp_path / "alerts.log",
        window_s=0.2,
        positions_every_s=0,
        resolutions_every_s=0,
        mtm_every_s=0,
        stats_every_s=0,
        drain_s=2,
        **cfg_kw,
    )
    return LiveRunner(
        cfg,
        Settings(),
        notifier=DryRunNotifier(cfg.alerts_log),
        rtds=FakeRtds(events),
        books=FakeBooks(),
        data_api=api(DataApi, "https://data.test/v2"),
        gamma=api(GammaApi, "https://gamma.test"),
    )


def test_runner_end_to_end_with_fakes(tmp_path):
    now = time.time()
    events = [
        trade("0xaa", "0x1", 100, now, "lead"),  # candidate: two fills of one order -> one signal + alert
        trade("0xaa", "0x1", 150, now, "lead"),
        trade("0xbb", "0x2", 250, now, "watcher"),  # watch tier: paper only
        trade("0xee", "0x3", 9999, now, "stranger"),  # not on the watchlist
    ]
    runner = make_runner(tmp_path, events, duration_s=1.0)
    stats = asyncio.run(runner.run())
    assert stats["dispatched"] == 2 and stats["engine"]["buy_orders"] == 2
    assert stats["engine"]["baseline@0s:filled"] == 2 and stats["alerts"] == {"alerts_signal": 1}
    assert stats["aggregator"] == {"fills_seen": 4, "fills_kept": 3, "orders_emitted": 2, "dropped": 0}

    alerts = (tmp_path / "alerts.log").read_text(encoding="utf-8")
    assert alerts.count("=====") == 2 and "<b>lead</b>" in alerts and "Will &lt;it&gt; happen?" in alerts
    assert "✅ куплено" in alerts and "watcher" not in alerts

    store = PaperStore(tmp_path / "paper.sqlite")
    lots = store.rows("SELECT tier, category, event_key, status FROM lots ORDER BY id")
    assert [(r["tier"], r["category"], r["event_key"], r["status"]) for r in lots] == [
        ("candidate", "politics", "ev:7", "filled"),
        ("watch", "politics", "ev:7", "filled"),
    ]
    orders = store.rows("SELECT wallet, size, n_fills, holdings_before FROM orders ORDER BY id")
    assert [tuple(r) for r in orders] == [("0xaa", 250.0, 2, 100.0), ("0xbb", 250.0, 1, 0.0)]
    [run] = store.rows("SELECT ended_ms, stats_json FROM runs")
    assert run["ended_ms"] and json.loads(run["stats_json"])["dispatched"] == 2


def test_runner_commands(tmp_path):
    runner = make_runner(tmp_path, [], control_sample=1)
    assert len(runner.watchlist) == 3 and runner.watchlist.get("0xcc").role == "control"

    async def run():
        out = {}
        for cmd in ("help", "status", "watchlist", "portfolio", "report", "pause", "nope"):
            out[cmd] = await runner.handle_command(cmd, [])
        paused = runner.engine.paused
        out["resume"] = await runner.handle_command("resume", [])
        await runner.aclose()
        return out, paused

    out, paused = asyncio.run(run())
    assert "/portfolio" in out["help"] and "Статус" in out["status"] and "lead" in out["watchlist"]
    assert "контроль" in out["watchlist"] and "Позиций нет" in out["portfolio"] and "Paper-отчёт" in out["report"]
    assert paused and not runner.engine.paused and "Неизвестная команда /nope" in out["nope"]
