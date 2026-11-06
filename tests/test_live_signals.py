import pytest

from sniper.live.rtds import TradeEvent
from sniper.live.signals import OrderAggregator


def ev(tx="0x1", wallet="0xaa", token="t1", side="BUY", size=10.0, price=0.5, ts=100, received=1000.0, fee=0.0):
    return TradeEvent(
        wallet=wallet,
        side=side,
        size=size,
        price=price,
        token_id=token,
        condition_id="0xc",
        outcome="Yes",
        outcome_index=0,
        tx_hash=tx,
        ts=ts,
        received_at=received,
        fee=fee,
    )


def test_fills_of_one_order_collapse_into_one_signal():
    agg = OrderAggregator(window_s=1.5, wallet_filter=frozenset({"0xaa"}).__contains__)
    assert agg.add(ev(size=10, price=0.50))
    assert agg.add(ev(size=30, price=0.52, received=1000.4, fee=0.1))
    assert agg.add(ev(side="SELL", size=5, price=0.6, received=1000.5))  # same tx, other side: another order
    assert not agg.add(ev(wallet="0xbb"))  # untracked wallet
    assert agg.pop_due(1001.0) == []
    [buy] = agg.pop_due(1001.5)
    assert (buy.side, buy.size, buy.n_fills) == ("BUY", 40, 2)
    assert buy.vwap == pytest.approx((10 * 0.50 + 30 * 0.52) / 40)
    assert buy.notional == pytest.approx(20.6) and buy.worst_price == 0.52
    assert (buy.first_received, buy.detected_at, buy.first_ts) == (1000.0, 1001.5, 100)
    assert buy.is_taker and buy.key == ("0x1", "0xaa", "t1", "BUY")
    [sell] = agg.pop_due(1002.0)
    assert sell.side == "SELL" and not sell.is_taker
    assert (agg.fills_seen, agg.fills_kept, agg.orders_emitted, len(agg)) == (4, 3, 2, 0)


def test_sell_worst_price_is_lowest_fill():
    agg = OrderAggregator()
    agg.add(ev(side="SELL", price=0.60, ts=101))
    agg.add(ev(side="SELL", price=0.55, ts=100))
    agg.add(ev(side="SELL", price=0.58, ts=102))
    [order] = agg.pop_all(2000.0)
    assert order.worst_price == 0.55 and (order.first_ts, order.last_ts) == (100, 102)
    assert order.detected_at == 2000.0


def test_orders_are_emitted_in_arrival_order_per_window():
    agg = OrderAggregator(window_s=1.0)
    agg.add(ev(tx="a", received=10.0))
    agg.add(ev(tx="b", received=10.6))
    agg.add(ev(tx="a", received=10.9, size=1))  # late fill of the first order, still inside its window
    assert [o.tx_hash for o in agg.pop_due(11.0)] == ["a"]
    assert [o.tx_hash for o in agg.pop_due(11.6)] == ["b"]


def test_debug_mode_keeps_every_wallet_with_a_memory_cap():
    agg = OrderAggregator(max_open=2)
    assert agg.add(ev(tx="a", wallet="0x01")) and agg.add(ev(tx="b", wallet="0x02"))
    assert not agg.add(ev(tx="c")) and agg.dropped == 1
    assert agg.add(ev(tx="a", wallet="0x01", size=1))  # an open order still aggregates


def test_position_gate_signals_once_per_position():
    from sniper.live.signals import LeaderOrder, PositionGate

    def order(side, size, price, tx):
        return LeaderOrder(
            tx_hash=tx, wallet="w", token_id="t", side=side, size=size, notional=size * price, worst_price=price
        )

    gate = PositionGate(min_notional=100)
    fragments = [gate.observe(order("BUY", 100, 0.08, f"0x{i}")) for i in range(5)]  # $8 maker fragments
    assert fragments == [None] * 5
    sig = gate.observe(order("BUY", 2000, 0.10, "0xadd"))
    assert (
        sig is not None and sig.entry_price == pytest.approx(240 / 2500) and sig.position_notional == pytest.approx(240)
    )
    assert sig.worst_price == 0.10 and sig.size == 2000  # liquidity and holdings come from the triggering order
    assert gate.observe(order("BUY", 1000, 0.12, "0xadd2")) is None  # later adds are not copied
    assert gate.observe(order("SELL", 3500, 0.2, "0xs")) is not None  # sells always pass
    assert gate.observe(order("BUY", 2000, 0.10, "0xnew")) is not None  # closed -> a new position signals again
    gate.seed("w", "u")
    assert (
        gate.observe(LeaderOrder(tx_hash="0x9", wallet="w", token_id="u", side="BUY", size=5000, notional=500)) is None
    )
