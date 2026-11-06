import asyncio

import httpx
import pytest

from sniper.http import ApiClient
from sniper.live.book import ClobBooks, Level, OrderBook

API_BOOK = {
    "market": "0xc",
    "asset_id": "tok",
    "timestamp": "1793984571026",
    "hash": "h",
    "bids": [{"price": "0.01", "size": "1450"}, {"price": "0.20", "size": "100"}, {"price": "0.21", "size": "137.5"}],
    "asks": [{"price": "0.99", "size": "1800"}, {"price": "0.25", "size": "200"}, {"price": "0.23", "size": "348.76"}],
    "min_order_size": "5",
    "tick_size": "0.01",
    "neg_risk": False,
    "last_trade_price": "0.220",
}


def test_from_api_normalises_to_best_first():
    book = OrderBook.from_api(API_BOOK | {"bids": API_BOOK["bids"] + [{"price": "0.5", "size": "0"}, {"bad": 1}]})
    assert [lv.price for lv in book.bids] == [0.21, 0.20, 0.01]
    assert [lv.price for lv in book.asks] == [0.23, 0.25, 0.99]
    assert (book.best_bid, book.best_ask) == (0.21, 0.23)
    assert book.mid == pytest.approx(0.22) and book.spread == pytest.approx(0.02)
    assert (book.min_order_size, book.tick_size, book.ts_ms) == (5.0, 0.01, 1793984571026)
    assert book.last_trade_price == 0.22 and book.token_id == "tok" and book.market == "0xc"
    assert book.top_levels(2) == {"bids": [[0.21, 137.5], [0.20, 100.0]], "asks": [[0.23, 348.76], [0.25, 200.0]]}


def test_empty_book():
    book = OrderBook.from_api({"bids": [], "asks": []})
    assert book.best_bid is None and book.best_ask is None and book.mid is None and book.spread is None
    assert book.depth_usdc("ask") == 0.0
    assert book.vwap_buy(10) == (None, 0.0, None, 0.0, False, ())


def test_depth_within_cents():
    book = OrderBook.from_api(API_BOOK)
    assert book.depth_usdc("ask", 0.02) == pytest.approx(0.23 * 348.76 + 0.25 * 200)
    assert book.depth_usdc("bid", 0.02) == pytest.approx(0.21 * 137.5 + 0.20 * 100)
    assert book.depth_usdc("ask", 0.0) == pytest.approx(0.23 * 348.76)
    assert book.usdc_up_to(0.25) == pytest.approx(0.23 * 348.76 + 0.25 * 200)
    with pytest.raises(ValueError):
        book.depth_usdc("buy")


def test_vwap_buy_walks_levels():
    book = OrderBook("t", asks=[Level(0.40, 100), Level(0.42, 100), Level(0.50, 1000)])
    fill = book.vwap_buy(70)  # $40 at 0.40 (100 sh), $30 at 0.42
    assert fill.complete and fill.usdc == pytest.approx(70)
    assert fill.shares == pytest.approx(100 + 30 / 0.42)
    assert fill.avg_price == pytest.approx(70 / (100 + 30 / 0.42))
    assert fill.worst_price == 0.42
    assert [(leg.price, round(leg.size, 6)) for leg in fill.legs] == [(0.40, 100.0), (0.42, round(30 / 0.42, 6))]


def test_vwap_buy_price_cap_and_thin_books():
    book = OrderBook("t", asks=[Level(0.40, 100), Level(0.42, 100), Level(0.50, 1000)])
    capped = book.vwap_buy(100, max_price=0.42)  # only $40 + $42 at or below the cap
    assert not capped.complete and capped.usdc == pytest.approx(82) and capped.shares == pytest.approx(200)
    assert capped.worst_price == 0.42
    assert book.vwap_buy(40, max_price=0.385 + 0.015).complete  # cap equal to a level is inclusive despite fp noise
    nothing = book.vwap_buy(10, max_price=0.39)
    assert nothing.avg_price is None and nothing.shares == 0 and not nothing.complete
    thin = OrderBook("t", asks=[Level(0.4, 10)]).vwap_buy(100)
    assert not thin.complete and thin.usdc == pytest.approx(4) and thin.shares == pytest.approx(10)


def test_vwap_sell_walks_bids_with_floor():
    book = OrderBook("t", bids=[Level(0.60, 50), Level(0.55, 100)])
    fill = book.vwap_sell(80)
    assert fill.complete and fill.shares == pytest.approx(80)
    assert fill.usdc == pytest.approx(50 * 0.60 + 30 * 0.55) and fill.worst_price == 0.55
    floor = book.vwap_sell(80, min_price=0.58)
    assert not floor.complete and floor.shares == pytest.approx(50)
    assert not book.vwap_sell(1000).complete


def test_fetch_book_timing_and_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["token_id"] == "tok":
            return httpx.Response(200, json=API_BOOK)
        return httpx.Response(404, json={"error": "No orderbook exists for the requested token id"})

    http = httpx.AsyncClient(base_url="https://clob.test", transport=httpx.MockTransport(handler))
    client = ApiClient("https://clob.test", general_limit=1000, client=http, max_retries=0)
    ticks = iter([10.0, 10.25, 20.0, 20.1])

    async def run():
        books = ClobBooks(client, clock=lambda: next(ticks))
        return await books.fetch("tok"), await books.fetch("nope"), books

    ok, missing, books = asyncio.run(run())
    assert ok.book is not None and ok.book.best_ask == 0.23 and ok.error is None
    assert (ok.req_start_ms, ok.req_end_ms, ok.latency_ms, ok.observed_at) == (10000, 10250, 250, 10.25)
    assert missing.book is None and missing.error.startswith("HTTP 404")
    assert (books.fetched, books.failed) == (2, 1)
