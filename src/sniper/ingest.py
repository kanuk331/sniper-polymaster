"""Data collection: candidate universe, wallet activity backfill, token/market enrichment, price windows."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime

from sniper.db import Database, activity_record
from sniper.polymarket import ClobApi, DataApi, GammaApi

log = logging.getLogger(__name__)


def snapshot_name(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"


async def universe_from_leaderboards(
    db: Database,
    api: DataApi,
    *,
    periods: Iterable[str],
    categories: Iterable[str],
    limit: int,
    snapshot: str,
) -> int:
    """Leaderboard candidates. Biased (selected on realized success): fine for live watchlists, not for backtests."""
    rows = []
    for period in periods:
        for category in categories:
            board = await api.leaderboard(time_period=period, category=category, sort_by="PNL", limit=limit)
            for e in board:
                rows.append(
                    {
                        "address": e["user_id"],
                        "user_name": e.get("user_name"),
                        "source": f"leaderboard:{period}:{category}",
                        "rank": e.get("rank"),
                        "pnl": e.get("pnl"),
                        "volume": e.get("volume"),
                    }
                )
            log.info("leaderboard %s/%s: %d rows", period, category, len(board))
    return db.add_universe(snapshot, rows)


async def universe_from_markets(
    db: Database,
    data_api: DataApi,
    gamma: GammaApi,
    *,
    end_date_min: str,
    end_date_max: str,
    n_markets: int,
    trades_per_market: int,
    min_notional: float,
    snapshot: str,
    concurrency: int = 4,
) -> int:
    """Wallets that traded the highest-volume markets that closed in a window.

    Selection does not look at outcomes or PnL, so a universe built from markets that closed before a backtest
    split avoids the survivorship/look-ahead bias of today's leaderboard.
    """
    markets = await gamma.closed_markets(end_date_min=end_date_min, end_date_max=end_date_max, limit=n_markets)
    log.info("markets closed in [%s, %s]: %d", end_date_min, end_date_max, len(markets))
    notional: dict[str, float] = defaultdict(float)
    names: dict[str, str] = {}
    sem = asyncio.Semaphore(concurrency)

    async def scan(market: dict) -> None:
        async with sem:
            async for t in data_api.trades(condition=market["conditionId"], max_items=trades_per_market):
                wallet = t["proxy_wallet"].lower()
                notional[wallet] += float(t["price"]) * float(t["size"])
                if t.get("name"):
                    names[wallet] = t["name"]

    await asyncio.gather(*(scan(m) for m in markets if m.get("conditionId")))
    source = f"markets:{end_date_min[:10]}..{end_date_max[:10]}"
    rows = [
        {"address": w, "user_name": names.get(w), "source": source, "volume": v}
        for w, v in notional.items()
        if v >= min_notional
    ]
    return db.add_universe(snapshot, rows)


async def backfill_wallet(db: Database, api: DataApi, wallet: str, *, since: int, max_items: int) -> int:
    state = db.sync_state(wallet)
    start = max(since, state.last_ts) if state and state.last_ts else since
    records = [activity_record(wallet, row) async for row in api.activity(wallet, start=start, max_items=max_items)]
    inserted = db.upsert_activity(records)
    last_ts = max((r["ts"] for r in records), default=state.last_ts if state else None)
    truncated = len(records) >= max_items or bool(state and state.truncated)
    db.set_sync_state(wallet, last_ts, len(db.activity(wallet)), truncated)
    return inserted


async def backfill(
    db: Database, api: DataApi, wallets: list[str], *, since: int, max_items: int, concurrency: int = 4
) -> dict[str, int]:
    sem = asyncio.Semaphore(concurrency)
    result: dict[str, int] = {}
    done = 0

    async def one(wallet: str) -> None:
        nonlocal done
        async with sem:
            try:
                result[wallet] = await backfill_wallet(db, api, wallet, since=since, max_items=max_items)
            except Exception as exc:  # one bad wallet must not abort a long backfill
                log.warning("backfill %s failed: %s", wallet, exc)
                result[wallet] = -1
            done += 1
            if done % 10 == 0 or done == len(wallets):
                log.info("backfill progress %d/%d", done, len(wallets))

    await asyncio.gather(*(one(w) for w in wallets))
    return result


async def enrich(db: Database, data_api: DataApi, gamma: GammaApi) -> tuple[int, int]:
    """Resolve token metadata (outcome, resolution, event, neg-risk) and market fee schedules/categories."""
    token_ids = db.token_ids_needing_refresh()
    log.info("enrich: %d tokens to refresh", len(token_ids))
    n_tokens = db.upsert_tokens(await data_api.tokens(token_ids)) if token_ids else 0
    needed = db.condition_ids_needing_market()
    log.info("enrich: %d markets to fetch", len(needed))
    n_markets = 0
    for closed in (True, False):
        ids = [c for c, is_closed in needed.items() if is_closed == closed]
        if ids:
            n_markets += db.upsert_markets(await gamma.markets_by_condition(ids, closed=closed))
    missing = set(needed) - set(db.markets())
    if missing:
        # Token and market closed flags can disagree briefly; retry the leftovers with both states.
        n_markets += db.upsert_markets(await gamma.markets_by_condition(missing))
    log.info("enrich: %d/%d tokens, %d/%d markets", n_tokens, len(token_ids), n_markets, len(needed))
    return n_tokens, n_markets


def merge_windows(
    points: Iterable[tuple[str, int]], pad_after: int, join_gap: int, max_len: int = 3 * 86400
) -> dict[str, list[tuple[int, int]]]:
    """Group (token, ts) needs into per-token windows [ts, ts + pad_after], joining windows closer than join_gap.

    CLOB rejects fidelity=1 ranges longer than ~7 days ("interval is too long"), so windows are capped at max_len.
    """
    by_token: dict[str, list[int]] = defaultdict(list)
    for token, ts in points:
        by_token[token].append(ts)
    out: dict[str, list[tuple[int, int]]] = {}
    for token, stamps in by_token.items():
        stamps.sort()
        windows: list[list[int]] = []
        for ts in stamps:
            if windows and ts <= windows[-1][1] + join_gap and ts + pad_after - windows[-1][0] <= max_len:
                windows[-1][1] = max(windows[-1][1], ts + pad_after)
            else:
                windows.append([ts, ts + pad_after])
        out[token] = [(a, b) for a, b in windows]
    return out


async def fetch_price_windows(
    db: Database, clob: ClobApi, windows: dict[str, list[tuple[int, int]]], *, concurrency: int = 8
) -> int:
    """Fetch ~1-minute CLOB price history for each window not already cached. Returns windows fetched."""
    todo = [(t, a, b) for t, ws in windows.items() for a, b in ws if not db.has_price_window(t, a, b)]
    sem = asyncio.Semaphore(concurrency)
    fetched = 0

    async def one(token: str, start: int, end: int) -> None:
        nonlocal fetched
        async with sem:
            try:
                points = await clob.prices_history(token, start=start - 120, end=end)
            except Exception as exc:
                log.warning("prices %s [%d,%d] failed: %s", token[:12], start, end, exc)
                return
            db.add_prices(token, start, end, points)
            fetched += 1
            if fetched % 200 == 0:
                log.info("price windows %d/%d", fetched, len(todo))

    await asyncio.gather(*(one(*w) for w in todo))
    return fetched
