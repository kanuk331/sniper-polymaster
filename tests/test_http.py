import asyncio
import json

import httpx
import pytest

from sniper.http import ApiClient, ApiError, encode_params


def make_client(handler, **kw) -> ApiClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="https://example.test/v2", transport=transport)
    return ApiClient("https://example.test/v2", general_limit=1000, client=http, backoff_base_s=0.0, **kw)


def test_encode_params():
    assert encode_params({"a": None, "b": True, "c": ["x", "y"], "d": 5}) == [("b", "true"), ("c", "x,y"), ("d", "5")]
    assert encode_params([("k", "1"), ("k", "2")]) == [("k", "1"), ("k", "2")]


def test_paginate_follows_cursor_and_resends_filters():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append((request.url.path, params))
        if "cursor" not in params:
            body = {"data": [{"i": 1}, {"i": 2}], "pagination": {"has_more": True, "next_cursor": "c1"}}
        else:
            body = {"data": [{"i": 3}], "pagination": {"has_more": False, "next_cursor": None}}
        return httpx.Response(200, json=body)

    async def run():
        client = make_client(handler)
        return [r["i"] async for r in client.paginate("/trades", {"user": "0xabc", "limit": 2})]

    assert asyncio.run(run()) == [1, 2, 3]
    assert seen[0][0] == "/v2/trades"
    assert seen[1][1] == {"user": "0xabc", "limit": "2", "cursor": "c1"}


def test_paginate_respects_max_items():
    def handler(request):
        return httpx.Response(
            200, json={"data": [{"i": i} for i in range(5)], "pagination": {"has_more": True, "next_cursor": "x"}}
        )

    async def run():
        client = make_client(handler)
        return [r async for r in client.paginate("/x", max_items=3)]

    assert len(asyncio.run(run())) == 3


def test_retries_on_429_with_retry_after():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return httpx.Response(200, json={"ok": True})

    async def run():
        return await make_client(handler).get("/x")

    assert asyncio.run(run()) == {"ok": True}
    assert calls["n"] == 3


def test_client_error_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, text=json.dumps({"error": "bad"}))

    async def run():
        await make_client(handler).get("/x")

    with pytest.raises(ApiError) as exc:
        asyncio.run(run())
    assert exc.value.status == 400
    assert calls["n"] == 1


def test_gives_up_after_max_retries():
    def handler(request):
        return httpx.Response(503, text="down")

    async def run():
        await make_client(handler, max_retries=2).get("/x")

    with pytest.raises(ApiError):
        asyncio.run(run())
