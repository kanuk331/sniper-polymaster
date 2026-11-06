"""Consumer for the global trade feed of Polymarket's real-time data service (RTDS).

`wss://ws-live-data.polymarket.com`, topic `activity`, type `trades`, no authentication. One message per fill per
participant: a taker order matching several makers arrives as several messages sharing a transaction hash, and each
maker appears with its own wallet and side. The server expects application-level text "ping" frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"
SUBSCRIBE = {"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]}
CONTROL_FRAMES = frozenset({"", "ping", "pong"})
SIDES = frozenset({"BUY", "SELL"})


@dataclass(frozen=True, slots=True)
class TradeEvent:
    wallet: str  # proxy wallet, lowercase
    side: str  # BUY / SELL from this wallet's point of view
    size: float  # shares
    price: float
    token_id: str
    condition_id: str
    outcome: str
    outcome_index: int
    tx_hash: str
    ts: int  # trade time from the payload, epoch seconds
    slug: str = ""
    event_slug: str = ""
    title: str = ""
    name: str = ""
    fee: float = 0.0  # taker fee this wallet paid on the fill (0 for makers and fee-free markets)
    received_at: float = 0.0  # local epoch seconds
    feed_ts_ms: int | None = None  # envelope timestamp: when RTDS emitted the message

    @property
    def notional(self) -> float:
        return self.size * self.price


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return "" if value is None else str(value)


def parse_trade(payload: Any, received_at: float = 0.0, feed_ts_ms: int | None = None) -> TradeEvent | None:
    """One `activity/trades` payload -> TradeEvent, or None if a required field is missing or invalid."""
    if not isinstance(payload, dict):
        return None
    try:
        side = str(payload["side"]).upper()
        size = float(payload["size"])
        price = float(payload["price"])
        ts = int(float(payload["timestamp"]))
        wallet = str(payload["proxyWallet"]).strip().lower()
        token_id = str(payload["asset"]).strip()
        tx_hash = str(payload["transactionHash"]).strip().lower()
        outcome_index = int(payload.get("outcomeIndex") or 0)
        fee = float(payload.get("fee") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None
    if side not in SIDES or not wallet.startswith("0x") or not token_id or not tx_hash:
        return None
    if not (size > 0) or not (0 < price <= 1):
        return None
    if ts > 10**11:  # tolerate milliseconds
        ts //= 1000
    return TradeEvent(
        wallet=wallet,
        side=side,
        size=size,
        price=price,
        token_id=token_id,
        condition_id=_text(payload, "conditionId").lower(),
        outcome=_text(payload, "outcome"),
        outcome_index=outcome_index,
        tx_hash=tx_hash,
        ts=ts,
        slug=_text(payload, "slug"),
        event_slug=_text(payload, "eventSlug"),
        title=_text(payload, "title"),
        name=_text(payload, "name") or _text(payload, "pseudonym"),
        fee=fee if fee > 0 else 0.0,
        received_at=received_at,
        feed_ts_ms=feed_ts_ms,
    )


def parse_message(raw: str | bytes, received_at: float | None = None) -> list[TradeEvent] | None:
    """Trades in one frame: [] for control frames and other topics, None for malformed frames."""
    if isinstance(raw, bytes | bytearray):
        try:
            raw = raw.decode()
        except UnicodeDecodeError:
            return None
    text = raw.strip()
    if text.lower() in CONTROL_FRAMES:
        return []
    try:
        msg = json.loads(text)
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("topic") != "activity" or msg.get("type") != "trades":
        return []
    feed_ts = msg.get("timestamp")
    feed_ts_ms = int(feed_ts) if isinstance(feed_ts, int | float) and not isinstance(feed_ts, bool) else None
    now = time.time() if received_at is None else received_at
    payload = msg.get("payload")
    items = payload if isinstance(payload, list) else [payload]
    events = [ev for p in items if (ev := parse_trade(p, now, feed_ts_ms)) is not None]
    if not events and items:
        return None
    return events


@dataclass
class RtdsStats:
    frames: int = 0
    trades: int = 0
    malformed: int = 0
    ignored: int = 0  # control frames and other topics
    connects: int = 0
    reconnects: int = 0
    connected_at: float = 0.0
    last_frame_at: float = 0.0


def _default_connect(url: str) -> Any:
    from websockets.asyncio.client import connect

    # Protocol pings off: the server's liveness contract is the text "ping"; a silent feed triggers a reconnect.
    return connect(url, ping_interval=None, max_size=2**22, open_timeout=15, close_timeout=5)


class RtdsClient:
    """Reconnecting RTDS subscriber. `events()` yields trades until `stop()` is called."""

    def __init__(
        self,
        url: str = RTDS_URL,
        *,
        ping_interval_s: float = 5.0,
        idle_timeout_s: float = 15.0,  # the healthy global feed carries ~35 messages/s
        backoff_base_s: float = 1.0,
        backoff_max_s: float = 60.0,
        connect: Callable[[str], Any] | None = None,
    ):
        self.url = url
        self.ping_interval_s = ping_interval_s
        self.idle_timeout_s = idle_timeout_s
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.stats = RtdsStats()
        self._connect = connect or _default_connect
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def _keepalive(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self.ping_interval_s)
            await ws.send("ping")

    async def events(self) -> AsyncIterator[TradeEvent]:
        failures = 0
        while not self._stopped:
            try:
                async with self._connect(self.url) as ws:
                    await ws.send(json.dumps(SUBSCRIBE))
                    self.stats.connects += 1
                    self.stats.connected_at = time.time()
                    log.info("RTDS connected (%s)", self.url)
                    pinger = asyncio.create_task(self._keepalive(ws))
                    try:
                        while not self._stopped:
                            raw = await asyncio.wait_for(ws.recv(), self.idle_timeout_s)
                            now = time.time()
                            self.stats.frames += 1
                            self.stats.last_frame_at = now
                            events = parse_message(raw, now)
                            if events is None:
                                self.stats.malformed += 1
                                continue
                            if not events:
                                self.stats.ignored += 1
                                continue
                            failures = 0
                            self.stats.trades += len(events)
                            for ev in events:
                                yield ev
                    finally:
                        pinger.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await pinger
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # ConnectionClosed, OSError, TimeoutError, handshake errors
                if self._stopped:
                    break
                log.warning("RTDS connection lost: %s: %s", type(exc).__name__, exc)
            if self._stopped:
                break
            delay = min(self.backoff_max_s, self.backoff_base_s * 2**failures) * (0.5 + random.random() / 2)
            failures += 1
            self.stats.reconnects += 1
            log.info("RTDS reconnect #%d in %.1fs", self.stats.reconnects, delay)
            await asyncio.sleep(delay)
