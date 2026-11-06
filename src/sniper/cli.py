"""Command-line entry point: `sniper <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from sniper import ingest, scoring
from sniper.config import Settings
from sniper.db import Database
from sniper.polymarket import DataApi, GammaApi

log = logging.getLogger("sniper")


def parse_date(text: str) -> int:
    """'2026-09-01' or epoch seconds -> epoch seconds (UTC)."""
    if text.isdigit():
        return int(text)
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp())


def _csv(text: str) -> list[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


async def cmd_universe(args: argparse.Namespace, settings: Settings, db: Database) -> None:
    data_api = DataApi.create(settings)
    try:
        if args.source == "leaderboard":
            snapshot = args.snapshot or ingest.snapshot_name("lb")
            n = await ingest.universe_from_leaderboards(
                db,
                data_api,
                periods=_csv(args.periods),
                categories=_csv(args.categories),
                limit=args.limit,
                snapshot=snapshot,
            )
        else:
            gamma = GammaApi.create(settings)
            try:
                snapshot = args.snapshot or ingest.snapshot_name("mk")
                n = await ingest.universe_from_markets(
                    db,
                    data_api,
                    gamma,
                    end_date_min=args.end_min,
                    end_date_max=args.end_max,
                    n_markets=args.markets,
                    trades_per_market=args.trades_per_market,
                    min_notional=args.min_notional,
                    snapshot=snapshot,
                )
            finally:
                await gamma.aclose()
    finally:
        await data_api.aclose()
    print(f"snapshot {snapshot}: {n} rows, {len(db.universe(snapshot))} distinct wallets")


async def cmd_backfill(args: argparse.Namespace, settings: Settings, db: Database) -> None:
    wallets = _csv(args.wallets) if args.wallets else db.universe(args.snapshot)
    if args.max_wallets:
        wallets = wallets[: args.max_wallets]
    if not wallets:
        sys.exit("no wallets: build a universe first (sniper universe ...) or pass --wallets")
    data_api = DataApi.create(settings)
    try:
        result = await ingest.backfill(
            db,
            data_api,
            [w.lower() for w in wallets],
            since=parse_date(args.since),
            max_items=args.max_items,
            concurrency=args.concurrency,
        )
    finally:
        await data_api.aclose()
    failed = sum(1 for v in result.values() if v < 0)
    print(
        f"backfilled {len(result) - failed}/{len(result)} wallets, {sum(v for v in result.values() if v > 0)} new rows,"
        f" {failed} failed, {data_api.client.requests_made} requests"
    )


async def cmd_enrich(args: argparse.Namespace, settings: Settings, db: Database) -> None:
    data_api, gamma = DataApi.create(settings), GammaApi.create(settings)
    try:
        n_tokens, n_markets = await ingest.enrich(db, data_api, gamma)
    finally:
        await data_api.aclose()
        await gamma.aclose()
    print(f"enriched {n_tokens} tokens, {n_markets} markets")


async def cmd_score(args: argparse.Namespace, settings: Settings, db: Database) -> None:
    wallets = _csv(args.wallets) if args.wallets else (db.universe(args.snapshot) or db.wallets_with_activity())
    as_of = parse_date(args.as_of) if args.as_of else None
    scores = scoring.score_wallets(db, [w.lower() for w in wallets], as_of=as_of)
    scoring.write_scores(scores, Path(args.out))
    print(json.dumps(scoring.tier_summary(scores), indent=1))
    for s in scores[: args.top]:
        r = scoring.score_row(s)
        print(
            f"{r['tier']:<12} {r['user_name'][:22]:<22} {r['wallet'][:10]} n={r['n_resolved']:<4} ev={r['n_events']:<4}"
            f" z={r['z']:<7} q={r['q_bh']:<7} θ̃={r['theta_shrunk']:<7} pnl={r['resolved_pnl']:<11} {r['flags']}"
        )
    print(f"written: {args.out}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sniper", description=__doc__)
    p.add_argument("--db", help="SQLite path (default: $SNIPER_DB_PATH or data/sniper.sqlite)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    u = sub.add_parser("universe", help="build a candidate-wallet universe snapshot")
    u.add_argument("source", choices=["leaderboard", "markets"])
    u.add_argument("--snapshot", help="snapshot name (default: generated)")
    u.add_argument("--periods", default="month,all", help="leaderboard periods: day,week,month,all")
    u.add_argument("--categories", default="overall", help="leaderboard categories, e.g. overall,politics,sports")
    u.add_argument("--limit", type=int, default=200, help="leaderboard rows per period/category")
    u.add_argument("--end-min", help="markets source: earliest market end date (ISO)")
    u.add_argument("--end-max", help="markets source: latest market end date (ISO)")
    u.add_argument("--markets", type=int, default=100, help="markets source: number of top-volume markets")
    u.add_argument("--trades-per-market", type=int, default=3000)
    u.add_argument("--min-notional", type=float, default=2000.0, help="markets source: min USDC traded per wallet")
    u.set_defaults(func=cmd_universe)

    b = sub.add_parser("backfill", help="download wallet activity (trades incl. maker fills, merges, redeems...)")
    b.add_argument("--snapshot", help="universe snapshot (default: latest)")
    b.add_argument("--wallets", help="comma-separated wallets instead of a snapshot")
    b.add_argument("--since", default="2026-01-01", help="ISO date or epoch seconds")
    b.add_argument("--max-items", type=int, default=20000, help="cap on activity rows per wallet per run")
    b.add_argument("--max-wallets", type=int)
    b.add_argument("--concurrency", type=int, default=4)
    b.set_defaults(func=cmd_backfill)

    e = sub.add_parser("enrich", help="fetch token resolutions and market fee schedules for stored activity")
    e.set_defaults(func=cmd_enrich)

    s = sub.add_parser("score", help="score wallets: archetype filters, skill test, FDR, shrinkage, tiers")
    s.add_argument("--snapshot", help="universe snapshot (default: latest; falls back to all wallets with activity)")
    s.add_argument("--wallets", help="comma-separated wallets")
    s.add_argument("--as-of", help="use only information before this date (ISO or epoch) - for walk-forward")
    s.add_argument("--out", default="reports/scores.csv", help=".csv or .json")
    s.add_argument("--top", type=int, default=25, help="rows to print")
    s.set_defaults(func=cmd_score)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    db = Database(args.db or settings.db_path)
    if args.command == "universe" and args.source == "markets" and not (args.end_min and args.end_max):
        sys.exit("universe markets requires --end-min and --end-max")
    try:
        asyncio.run(args.func(args, settings, db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
