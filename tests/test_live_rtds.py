import asyncio
import json

import pytest

from sniper.live.rtds import RtdsClient, parse_message, parse_trade

PAYLOAD = {
    "asset": "22499342068371127005250815750408092280711418731583169430103581405577478463688",
    "conditionId": "0xABC123",
    "eventSlug": "some-event",
    "fee": 0.01366,
    "name": "ladsontop",
    "outcome": "Down",
    "outcomeIndex": 1,
    "price": 0.96,
    "proxyWallet": "0x04565C2220F77897222Dec11f610E7a9535b3235",
    "pseudonym": "Entire-Thunder",
    "side": "BUY",
    "size": 5.083325,
    "slug": "some-market",
    "timestamp": 1793984585,
    "title": "Hyperliquid Up or Down",
    "transactionHash": "0x21AFbc80",
}


def frame(payload=PAYLOAD, **kw) -> str:
    msg = {"connection_id": "c1", "payload": payload, "timestamp": 1793984585088, "topic": "activity", "type": "trades"}
    return json.dumps(msg | kw)


def test_parse_message_trade_fields():
    [ev] = parse_message(frame(), received_at=1793984586.0)
    assert ev.wallet == "0x04565c2220f77897222dec11f610e7a9535b3235"
    assert (ev.side, ev.size, ev.price) == ("BUY", 5.083325, 0.96)
    assert ev.token_id == PAYLOAD["asset"] and ev.condition_id == "0xabc123"
    assert (ev.outcome, ev.outcome_index) == ("Down", 1)
    assert ev.tx_hash == "0x21afbc80"
    assert (ev.ts, ev.feed_ts_ms, ev.received_at) == (1793984585, 1793984585088, 1793984586.0)
    assert ev.fee == 0.01366 and ev.notional == pytest.approx(5.083325 * 0.96)
    assert (ev.slug, ev.event_slug, ev.title, ev.name) == (
        "some-market",
        "some-event",
        "Hyperliquid Up or Down",
        "ladsontop",
    )


def test_parse_trade_defaults_and_name_fallback():
    ev = parse_trade(PAYLOAD | {"name": "", "fee": None, "outcomeIndex": None}, 1.0)
    assert ev is not None and ev.name == "Entire-Thunder" and ev.fee == 0.0 and ev.outcome_index == 0


def test_control_frames_and_other_topics_are_ignored():
    assert parse_message("pong") == []
    assert parse_message("") == []
    assert parse_message(b" PING ") == []
    assert parse_message(json.dumps({"topic": "comments", "type": "comment_created", "payload": {}})) == []
    assert parse_message(json.dumps({"statusCode": 400, "body": "bad subscription"})) == []


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        b"\xff\xfe",
        frame(payload=None),
        frame(payload=PAYLOAD | {"side": "HOLD"}),
        frame(payload=PAYLOAD | {"size": 0}),
        frame(payload=PAYLOAD | {"size": "nan"}),
        frame(payload=PAYLOAD | {"price": "abc"}),
        frame(payload=PAYLOAD | {"price": 1.5}),
        frame(payload={k: v for k, v in PAYLOAD.items() if k != "transactionHash"}),
        frame(payload=PAYLOAD | {"proxyWallet": "nobody"}),
    ],
)
def test_malformed_frames(raw):
    assert parse_message(raw) is None


def test_batch_payload_keeps_valid_items_and_ms_timestamps():
    events = parse_message(frame(payload=[PAYLOAD | {"timestamp": 1793984585123}, {"junk": 1}]))
    assert len(events) == 1 and events[0].ts == 1793984585


class FakeWs:
    def __init__(self, frames: list[str], *, hang: bool = False, delay: float = 0.0):
        self.frames = list(frames)
        self.hang = hang
        self.delay = delay
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    async def recv(self) -> str:
        if self.hang:
            await asyncio.sleep(3600)
        await asyncio.sleep(self.delay)
        if self.frames:
            return self.frames.pop(0)
        raise ConnectionError("closed by server")


def run_client(client: RtdsClient, n: int) -> list[str]:
    async def run():
        out = []
        async for ev in client.events():
            out.append(ev.tx_hash)
            if len(out) == n:
                break
        return out

    return asyncio.run(run())


def test_client_counts_and_reconnects_after_close():
    first = FakeWs([frame(), "pong", "garbage"])
    second = FakeWs([frame(payload=PAYLOAD | {"transactionHash": "0x2"})])
    sessions = [first, second]
    client = RtdsClient(connect=lambda url: sessions.pop(0), backoff_base_s=0.0, ping_interval_s=60)
    assert run_client(client, 2) == ["0x21afbc80", "0x2"]
    st = client.stats
    assert (st.connects, st.reconnects, st.trades, st.malformed, st.ignored) == (2, 1, 2, 1, 1)
    assert json.loads(first.sent[0]) == {
        "action": "subscribe",
        "subscriptions": [{"topic": "activity", "type": "trades"}],
    }


def test_client_reconnects_on_connect_error_and_idle_feed():
    attempts = []

    def connect(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("connection refused")
        if len(attempts) == 2:
            return FakeWs([], hang=True)
        return FakeWs([frame()])

    client = RtdsClient(connect=connect, backoff_base_s=0.0, idle_timeout_s=0.05, ping_interval_s=60)
    assert run_client(client, 1) == ["0x21afbc80"]
    assert len(attempts) == 3 and client.stats.reconnects == 2


def test_client_sends_text_keepalive():
    ws = FakeWs([frame()], delay=0.05)
    client = RtdsClient(connect=lambda url: ws, ping_interval_s=0.01)
    run_client(client, 1)
    assert "ping" in ws.sent[1:]
