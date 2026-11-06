"""Multi-period walk-forward: data collection, evaluation and cross-period reporting."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from pathlib import Path
from typing import Any

from sniper import ingest
from sniper.db import Database
from sniper.plan import Period
from sniper.polymarket import DataApi, GammaApi

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
