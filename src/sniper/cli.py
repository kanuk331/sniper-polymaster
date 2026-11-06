"""Command-line entry point: `sniper <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from sniper import backtest, ingest, plan, scoring, walkforward
from sniper.config import Settings
from sniper.db import Database
from sniper.polymarket import ClobApi, DataApi, GammaApi

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


async def cmd_backtest(args: argparse.Namespace, settings: Settings, db: Database) -> None:
    wallets = _csv(args.wallets) if args.wallets else (db.universe(args.snapshot) or db.wallets_with_activity())
    split = parse_date(args.split)
    end = split + int(args.horizon_days * 86400)
    latencies = [int(x) for x in _csv(args.latencies)]
    base = backtest.CopyParams(
        stake_usdc=args.stake, half_spread=args.half_spread, max_premium=args.max_premium, max_signals=args.max_signals
    )
    meta, scores = backtest.walk_forward_scores(
        db, [w.lower() for w in wallets], split_ts=split, gap_days=args.gap_days
    )
    chosen = backtest.choose_wallets_to_simulate(
        scores, include_excluded=args.sample_excluded, include_noise=args.sample_noise, seed=args.seed
    )
    print(f"scored {len(scores)} wallets as of split-{args.gap_days}d: {scoring.tier_summary(scores)['tiers']}")
    print(f"simulating {len(chosen)} wallets over [{args.split}, +{args.horizon_days}d], latencies {latencies}")

    points = []
    for s in chosen:
        pts = backtest.signal_points(db.activity(s.wallet, until=end), split, end)
        points += pts[: args.max_signals * 3]
    windows = ingest.merge_windows(points, pad_after=max(latencies) + 600, join_gap=3600)
    clob = ClobApi.create(settings)
    try:
        fetched = await ingest.fetch_price_windows(db, clob, windows, concurrency=args.concurrency)
    finally:
        await clob.aclose()
    series = {}
    for token, ws in windows.items():
        series[token] = db.prices(token, min(a for a, _ in ws) - 120, max(b for _, b in ws))
    book = backtest.PriceBook(series)
    print(f"price windows: {sum(len(w) for w in windows.values())} needed, {fetched} fetched now")

    results = backtest.run_simulations(
        db, meta, chosen, start=split, end=end, latencies=latencies, base=base, price_book=book
    )
    report = {
        "params": {
            "split": args.split,
            "gap_days": args.gap_days,
            "horizon_days": args.horizon_days,
            "latencies": latencies,
            "copy": base.__dict__,
            "wallets_scored": len(scores),
            "wallets_simulated": len(chosen),
        },
        "score_tiers": scoring.tier_summary(scores),
        "summary": [ts.__dict__ for lat in latencies for ts in backtest.summarize(results, lat)],
        "predictive": [backtest.predictive_check(results, lat) for lat in latencies],
        "wallets": [scoring.score_row(s) | {"copy": r.as_dict()} for s, r in results],
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=1, default=str))
    print(
        f"{'tier':<12} {'lat':>4} {'wal':>4} {'fill':>5} {'cost$':>9} {'pnl$':>9} {'roi':>7} {'lead_roi':>8}"
        f" {'eff':>6} {'%prof':>6} {'prem¢':>6}"
    )
    for row in report["summary"]:
        eff = row["copy_efficiency"]
        prof = row["share_wallets_profitable"]
        print(
            f"{row['tier']:<12} {row['latency_s']:>4} {row['wallets']:>4} {row['filled']:>5} {row['cost']:>9.0f}"
            f" {row['pnl']:>9.0f} {row['roi']:>7.3f} {row['leader_roi']:>8.3f}"
            f" {eff if eff is None else round(eff, 2)!s:>6} {prof if prof is None else round(prof, 2)!s:>6}"
            f" {row['mean_premium'] * 100:>6.2f}"
        )
    for p in report["predictive"]:
        print(f"predictive (latency {p['latency_s']}s): {p}")
    print(f"written: {args.out}")


async def cmd_wf_collect(args: argparse.Namespace, settings: Settings, db: Database) -> None:
    periods = plan.select(plan.load_plan(args.plan), role=args.role, names=_csv(args.periods) if args.periods else None)
    data_api, gamma = DataApi.create(settings), GammaApi.create(settings)
    try:
        for period in periods:
            summary = await walkforward.collect(db, period, data_api, gamma, Path(args.out))
            print(json.dumps(summary))
    finally:
        await data_api.aclose()
        await gamma.aclose()


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

    t = sub.add_parser("backtest", help="walk-forward: score before --split, simulate copying after it")
    t.add_argument("--snapshot")
    t.add_argument("--wallets")
    t.add_argument("--split", required=True, help="ISO date: scoring uses data before split - gap")
    t.add_argument("--gap-days", type=float, default=7.0)
    t.add_argument("--horizon-days", type=float, default=45.0)
    t.add_argument("--latencies", default="10,60,300,900", help="follower delays in seconds")
    t.add_argument("--stake", type=float, default=100.0, help="USDC per copied signal")
    t.add_argument("--half-spread", type=float, default=0.01)
    t.add_argument("--max-premium", type=float, default=0.015, help="skip if price moved more than this vs leader")
    t.add_argument("--max-signals", type=int, default=300, help="per wallet")
    t.add_argument("--sample-noise", type=int, default=150, help="noise/insufficient wallets simulated per tier")
    t.add_argument("--sample-excluded", type=int, default=150)
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--concurrency", type=int, default=8)
    t.add_argument("--out", default="reports/backtest.json")
    t.set_defaults(func=cmd_backtest)

    wf = sub.add_parser("wf", help="multi-period walk-forward (config/walkforward.toml)")
    wf_sub = wf.add_subparsers(dest="wf_command", required=True)
    wc = wf_sub.add_parser("collect", help="universe + windowed backfill + enrichment for plan periods")
    wc.add_argument("--plan", default="config/walkforward.toml")
    wc.add_argument("--role", choices=["dev", "holdout", "all"], default="all")
    wc.add_argument("--periods", help="comma-separated period names (default: all with the role)")
    wc.add_argument("--out", default="reports/walkforward")
    wc.set_defaults(func=cmd_wf_collect)
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
