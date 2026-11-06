"""Multi-period walk-forward: data collection, evaluation and cross-period reporting."""

from __future__ import annotations

import csv
import gzip
import json
import logging
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from sniper import backtest, ingest, metrics
from sniper.analytics import Meta
from sniper.config import Settings
from sniper.db import Database
from sniper.metrics import Lot
from sniper.plan import Period
from sniper.polymarket import ClobApi, DataApi, GammaApi
from sniper.scoring import WalletScore

log = logging.getLogger(__name__)

PHASE1_BACKFILL_SINCE = 1767225600  # 2026-01-01, the --since used for the phase-1 backfill


def period_dir(root: Path, period: Period) -> Path:
    d = root / period.name
    d.mkdir(parents=True, exist_ok=True)
    return d


async def collect(db: Database, period: Period, data_api: DataApi, gamma: GammaApi, out_root: Path) -> dict[str, Any]:
    """Universe (markets closed inside the lookback) -> windowed activity backfill -> token/market enrichment."""
    t0 = time.time()
    migrated = db.migrate_sync_state(PHASE1_BACKFILL_SINCE)
    if migrated:
        log.info("migrated %d phase-1 sync_state rows to coverage", migrated)
    wallets = db.universe(period.snapshot)
    if not wallets:
        end_min, end_max = period.universe_end_dates
        await ingest.universe_from_markets(
            db,
            data_api,
            gamma,
            end_date_min=end_min,
            end_date_max=end_max,
            n_markets=period.universe_markets,
            trades_per_market=period.trades_per_market,
            min_notional=period.min_notional,
            snapshot=period.snapshot,
        )
        wallets = db.universe(period.snapshot)
    log.info("%s: universe %d wallets", period.name, len(wallets))
    start, end = period.window
    requests_before = data_api.client.requests_made
    status = await ingest.backfill_windows(db, data_api, wallets, start, end, cap=period.activity_cap)
    n_tokens, n_markets = await ingest.enrich(db, data_api, gamma)
    summary = {
        "period": period.name,
        "wallets": len(wallets),
        "window": [start, end],
        "status": dict(Counter(status.values())),
        "data_api_requests": data_api.client.requests_made - requests_before,
        "tokens_enriched": n_tokens,
        "markets_enriched": n_markets,
        "elapsed_s": round(time.time() - t0, 1),
    }
    (period_dir(out_root, period) / "collect.json").write_text(json.dumps(summary, indent=1))
    log.info("%s collected: %s", period.name, summary)
    return summary


# --- shared helpers ------------------------------------------------------------------------------------------------


async def price_book_for(
    db: Database,
    settings: Settings,
    scores: Sequence[WalletScore],
    start: int,
    end: int,
    meta: Meta,
    *,
    pad_after: int,
    max_points_per_wallet: int | None = None,
) -> backtest.PriceBook:
    """Fetch (or reuse cached) ~1-minute CLOB prices around every trade of these wallets in [start, end)."""
    points: list[tuple[str, int]] = []
    for s in scores:
        pts = backtest.signal_points(db.activity(s.wallet, until=end), start, end, meta)
        points += pts[-max_points_per_wallet:] if max_points_per_wallet else pts
    windows = ingest.merge_windows(points, pad_after=pad_after, join_gap=3600)
    clob = ClobApi.create(settings)
    try:
        fetched = await ingest.fetch_price_windows(db, clob, windows)
    finally:
        await clob.aclose()
    log.info("price windows: %d needed, %d fetched", sum(len(w) for w in windows.values()), fetched)
    series = {t: db.prices(t, min(a for a, _ in ws) - 120, max(b for _, b in ws)) for t, ws in windows.items()}
    return backtest.PriceBook(series)


def write_lots(lots: Iterable[Lot], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = metrics.lots_to_rows(lots)
    if not rows:
        path.write_bytes(b"")
        return
    with gzip.open(path, "wt", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows: Sequence[dict[str, Any]], keys: Sequence[str] = ("tier", "latency_s")) -> None:
    head = " ".join(f"{k:<14}" for k in keys)
    print(
        f"{head} {'sig':>6} {'fill':>6} {'miss%':>6} {'cost$':>9} {'fees$':>7} {'slip¢':>6} {'pnl$':>9} "
        f"{'roi':>7} {'ci95':>18} {'lead':>7} {'peak$':>8} {'dd$':>7}"
    )
    for r in rows:
        key = " ".join(f"{r[k]!s:<14}" for k in keys)
        print(
            f"{key} {r['signals']:>6} {r['filled']:>6} {r['missed_share']:>6.1%} {r['cost']:>9.0f} {r['fees']:>7.0f}"
            f" {r['slippage_cents']:>6.2f} {r['pnl']:>9.0f} {r['roi']:>7.1%} {metrics.fmt_ci(r['roi_ci95']):>18}"
            f" {r['leader_roi']:>7.1%} {r['peak_capital']:>8.0f} {r['max_drawdown']:>7.0f}"
        )
