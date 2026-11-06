import asyncio
import json
import logging
import time

import httpx

from sniper.live import messages
from sniper.live.book import BookSnapshot, Level, OrderBook
from sniper.live.markets import MarketInfo
from sniper.live.paper import EntryOutcome, ExitOutcome, Variant
from sniper.live.signals import LeaderOrder
from sniper.live.telegram import DryRunNotifier, TelegramNotifier, fit_message, parse_command
from sniper.live.watchlist import WatchEntry
from sniper.metrics import Lot

TOKEN = "123456:SECRET-token"
WALLET = "0x1ee9a5fc09665909c0cce297c581703bfbb9197f"


def make(handler, chat="42", **kw) -> TelegramNotifier:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kw.setdefault("min_interval_s", 0.0)
    return TelegramNotifier(TOKEN, chat, client=client, **kw)


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


def test_send_message_payload():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return ok(request)

    async def run():
        tg = make(handler)
        assert await tg.send_now("<b>hi</b> &amp; bye")
        await tg.aclose()
        return tg

    tg = asyncio.run(run())
    [(method, path, body)] = seen
    assert (method, path) == ("POST", f"/bot{TOKEN}/sendMessage")
    assert body == {
        "chat_id": "42",
        "text": "<b>hi</b> &amp; bye",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    assert tg.sent == 1


def test_commands_accepted_only_from_configured_chat():
    replies = []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/getUpdates"):
            assert body["timeout"] == 25 and "offset" not in body
            updates = [
                {"update_id": 5, "message": {"chat": {"id": 999}, "text": "/pause"}},
                {"update_id": 6, "message": {"chat": {"id": 42}, "text": "/status@SniperBot now"}},
                {"update_id": 7, "message": {"chat": {"id": 42}, "text": "hello"}},
            ]
            return httpx.Response(200, json={"ok": True, "result": updates})
        replies.append(body)
        return ok(request)

    calls = []

    async def on_command(name, args):
        calls.append((name, args))
        return "статус <b>ok</b>"

    async def run():
        tg = make(handler)
        handled = await tg.handle_updates(await tg.get_updates(), on_command)
        await tg.flush(2)
        await tg.aclose()
        return tg, handled

    tg, handled = asyncio.run(run())
    assert calls == [("status", ["now"])] and handled == 1
    assert tg.offset == 8 and tg.ignored_updates == 1
    assert [(r["chat_id"], r["text"]) for r in replies] == [("42", "статус <b>ok</b>")]


def test_parse_command():
    assert parse_command("/Report@bot x y") == ("report", ["x", "y"])
    assert parse_command("hello /status") is None
    assert parse_command("/") is None


def test_retry_after_429_and_min_interval_between_messages():
    stamps = []
    responses = iter([httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})])

    def handler(request):
        stamps.append(time.monotonic())
        resp = next(responses, None)
        return resp if resp is not None else ok(request)

    async def run():
        tg = make(handler, min_interval_s=0.05)
        for i in range(3):
            await tg.send(f"m{i}")
        await tg.flush(5)
        await tg.aclose()
        return tg

    tg = asyncio.run(run())
    assert (tg.sent, tg.failed, len(stamps)) == (3, 0, 4)
    assert min(b - a for a, b in zip(stamps, stamps[1:], strict=False)) >= 0.045


def test_queue_drops_oldest_when_full():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content)["text"])
        return ok(request)

    async def run():
        tg = make(handler, max_queue=2)
        for text in ("a", "b", "c"):  # the worker has not run yet: the queue overflows
            await tg.send(text)
        await tg.flush(2)
        await tg.aclose()
        return tg

    tg = asyncio.run(run())
    assert sent == ["b", "c"] and tg.dropped == 1


def test_errors_never_expose_the_token(caplog):
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    async def run():
        tg = make(handler)
        sent = await tg.send_now("x")
        failed_updates = await tg.get_updates()
        await tg.aclose()
        return sent, failed_updates

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(run()) == (False, None)
    assert "SECRET" not in caplog.text and "***" in caplog.text


def test_from_env(monkeypatch):
    monkeypatch.delenv("SNIPER_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SNIPER_TELEGRAM_CHAT_ID", raising=False)
    assert TelegramNotifier.from_env() is None
    monkeypatch.setenv("SNIPER_TELEGRAM_BOT_TOKEN", "1:abc")
    monkeypatch.setenv("SNIPER_TELEGRAM_CHAT_ID", "-1001")
    tg = TelegramNotifier.from_env()
    assert tg is not None and tg.chat_id == "-1001"
    asyncio.run(tg.aclose())


def test_dry_run_notifier_appends_to_log(tmp_path, caplog):
    path = tmp_path / "alerts.log"

    async def run():
        n = DryRunNotifier(path)
        await n.send("first <b>x</b>")
        await n.send("second")
        await n.run_commands(None)
        return n

    with caplog.at_level(logging.INFO):
        n = asyncio.run(run())
    text = path.read_text(encoding="utf-8")
    assert n.sent == 2 and "first <b>x</b>" in text and text.count("=====") == 4
    assert "second" in caplog.text


def test_fit_message_cuts_at_a_line_boundary():
    text = "\n".join(f"<b>line {i}</b>" for i in range(1000))
    cut = fit_message(text, 100)
    assert len(cut) <= 100 and cut.endswith("\n…") and cut.count("<b>") == cut.count("</b>")


def outcome(title="Will X?", name="vinii", status="filled", asks=((0.40, 1000),)) -> EntryOutcome:
    order = LeaderOrder(
        tx_hash="0x1",
        wallet=WALLET,
        token_id="t1",
        side="BUY",
        condition_id="0xc",
        outcome="Yes <maybe>",
        title=title,
        event_slug='ev"x',
        name=name,
        size=250,
        notional=100,
        fee=0.5,
        worst_price=0.41,
        n_fills=2,
        first_ts=1000,
    )
    entry = WatchEntry(WALLET, "candidate", "alert", user_name=name, theta_shrunk=0.2126)
    b = OrderBook("t1", bids=[Level(0.38, 1000)], asks=[Level(*a) for a in asks])
    lot = Lot(
        period="live", strategy="baseline", wallet=WALLET, user_name=name, tier="candidate", style="",
        category="politics", event_key="ev:1", condition_id="0xc", token_id="t1", latency_s=0, signal_ts=1000,
        leader_price=0.40, leader_worst=0.41, leader_notional=100, status=status,
    )  # fmt: skip
    if status == "filled":
        lot.shares, lot.cost, lot.exec_price, lot.entry_fee = 250.0, 100.0, 0.40, 2.4
    return EntryOutcome(
        order, 1, entry, Variant("baseline", 0), lot, MarketInfo("t1", "0xc"),
        BookSnapshot("t1", b, 1_004_000, 1_004_150),
        stake=100, probe_stake=100, cap=0.415, probe=b.vwap_buy(100, 0.415), uncapped=b.vwap_buy(100),
        usage={"market": 0.5, "event": 0.2, "leader": 0.0667, "category": 0.0333, "total": 0.02},
    )  # fmt: skip


def test_signal_alert_escapes_html_and_shows_the_decision():
    text = messages.render_signal(outcome("Will <b>X</b> & Y?", "A<script>"), now=1005)
    assert "Will &lt;b&gt;X&lt;/b&gt; &amp; Y?" in text and "A&lt;script&gt;" in text and "<script>" not in text
    assert 'href="https://polymarket.com/event/ev&quot;x"' in text and "Yes &lt;maybe&gt;" in text
    assert "0x1ee9…197f" in text and "θ̃ +0.213" in text and "5 с назад" in text and "тейкер" in text
    assert "Лидер: 250.0 sh × 0.400 (худшая 0.410) = $100.00" in text
    assert "bid 0.380 / ask 0.400" in text and "спред 2.0¢" in text and "(150 мс)" in text
    assert "VWAP 0.4000 (+0.0¢ к лидеру)" in text and "✅ куплено 250.0 sh" in text and "рынок 50%" in text
    skipped = messages.render_signal(outcome(status="skip:premium", asks=((0.42, 1000),)), now=1005)
    assert "⏭ пропуск — цена ушла" in skipped and "ask 0.420 &gt; 0.415" in skipped
    assert "VWAP 0.4200 (+2.0¢ к лидеру)" in skipped


def test_other_messages_render_and_escape():
    o = outcome()
    exit_text = messages.render_exit(
        ExitOutcome(Variant("baseline", 0), WALLET, "t1", "sell", 0.25, status="ok", shares=62.5, proceeds=30, fee=0.6,
                    basis=25, lots=1),
        o.order, o.entry, "A & B · Yes",
    )  # fmt: skip
    assert "Лидер продаёт" in exit_text and "A &amp; B" in exit_text and "PnL +$4.40" in exit_text
    assert "25% позиции" in exit_text
    assert "kill-switch" in messages.render_kill_switch(Variant("baseline", 0), 8500, 10_000).lower()
    report = {
        "by_latency": [
            {"latency_s": 0, "variant": "baseline@0s", "signals": 3, "filled": 2, "cost": 200, "pnl": -5.5,
             "roi": -0.0275, "roi_ci95": (-0.1, 0.05), "fees": 4.8, "slippage_cents": 0.4, "peak_capital": 200,
             "max_drawdown": 10, "missed_by_reason": {"premium": 1}},
        ],
        "mtm": {"baseline@0s": {"max_drawdown": 12.5}},
    }  # fmt: skip
    text = messages.render_report(report)
    assert (
        "ROI −2.8% [−10.0%; +5.0%]".replace("−", "-") in text and "premium 1" in text and "MTM-просадка $12.50" in text
    )
    wl = messages.render_watchlist([WatchEntry(WALLET, "candidate", "alert", user_name="<vinii>", theta_shrunk=0.21)])
    assert "&lt;vinii&gt;" in wl and "алерты" in wl
