"""`sniper live ...`: real-time Telegram alerts and paper trading. No live trading.

Wire into the main CLI with `sniper.live.cli.register(sub)`; standalone: `python -m sniper.live.cli live run ...`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from sniper.config import Settings
from sniper.live import report
from sniper.live.paper import PaperConfig
from sniper.live.risk import RiskConfig
from sniper.live.runner import LiveRunner, RunConfig
from sniper.live.store import PaperStore


def _csv(text: str) -> list[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


def run_config(args: argparse.Namespace) -> RunConfig:
    risk = RiskConfig(
        bankroll=args.bankroll, stake_mode=args.stake_mode, stake_usdc=args.stake, kelly_fraction=args.kelly_fraction
    )
    paper = PaperConfig(
        strategy=args.strategy or ("position" if args.signal_mode == "position" else "baseline"),
        delays=tuple(int(x) for x in _csv(args.delays)),
        max_premium=args.max_premium,
        min_fill_ratio=args.min_fill_ratio,
        risk=risk,
    )
    return RunConfig(
        scores=Path(args.scores),
        alert_tiers=tuple(_csv(args.alert_tiers)),
        track_tiers=tuple(_csv(args.track_tiers)),
        control_sample=args.control_sample,
        seed=args.seed,
        paper=paper,
        paper_db=Path(args.paper_db),
        alerts_log=Path(args.alerts_log),
        duration_s=args.duration,
        window_s=args.window,
        debug_min_usdc=args.debug_watch_all_min_usdc,
        debug_alert=args.debug_alert,
        force_dry_run=args.dry_run,
        alert_exits=not args.no_exit_alerts,
        positions_every_s=args.positions_every,
        resolutions_every_s=args.resolutions_every,
        mtm_every_s=args.mtm_every,
        drain_s=args.drain,
        signal_mode=args.signal_mode,
        min_leader_notional=args.min_leader_notional,
    )


def _check_paper_db(path: Path, settings: Settings, research_db: str | None = None) -> None:
    for other in (settings.db_path, research_db):
        if other and path.resolve() == Path(other).resolve():
            sys.exit(f"--paper-db {path} is the research database; use a separate file")


async def cmd_live_run(args: argparse.Namespace, settings: Settings, db: Any = None) -> None:
    cfg = run_config(args)
    if not cfg.scores.exists():
        sys.exit(f"scores file not found: {cfg.scores} (run `sniper score` first)")
    if not cfg.paper.delays:
        sys.exit("--delays must list at least one delay")
    _check_paper_db(cfg.paper_db, settings, getattr(args, "db", None))
    stats = await LiveRunner(cfg, settings).run()
    print(json.dumps(stats, indent=1, default=str))


async def cmd_live_report(args: argparse.Namespace, settings: Settings, db: Any = None) -> None:
    path = Path(args.paper_db)
    if not path.exists():
        sys.exit(f"paper database not found: {path}")
    _check_paper_db(path, settings, getattr(args, "db", None))
    store = PaperStore(path)
    try:
        rep = report.build_report(store, strategy=args.strategy, n_boot=args.n_boot, min_signals=args.min_signals)
    finally:
        store.close()
    print(report.format_report(rep))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(rep, indent=1, default=str))
        print(f"written: {args.out}")


def register(subparsers: Any) -> argparse.ArgumentParser:
    live = subparsers.add_parser("live", help="phase 2: Telegram alerts + paper trading on live order books")
    sub = live.add_subparsers(dest="live_command", required=True)

    r = sub.add_parser("run", help="stream trades, snapshot order books, paper-trade tracked wallets, send alerts")
    r.add_argument("--scores", default="reports/scores-now.csv", help="scores CSV from `sniper score`")
    r.add_argument("--alert-tiers", default="candidate", help="tiers that trigger Telegram alerts")
    r.add_argument("--track-tiers", default="candidate,watch", help="tiers that are paper-traded (no alerts)")
    r.add_argument("--control-sample", type=int, default=0, help="N seeded random noise-tier wallets, paper only")
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--delays", default="0,10,60,300", help="reaction delays (s); each is its own paper portfolio")
    r.add_argument(
        "--signal-mode", choices=["position", "order"], default="position",
        help="position: copy only position entries once the leader's open cost reaches --min-leader-notional "
        "(primary, pre-registered); order: copy every leader order (phase-1 baseline)",
    )  # fmt: skip
    r.add_argument("--min-leader-notional", type=float, default=100.0)
    r.add_argument("--strategy", help="strategy label stored on every lot (default: position / baseline by mode)")
    r.add_argument("--stake", type=float, default=100.0, help="USDC per copied order (fixed mode)")
    r.add_argument("--stake-mode", choices=["fixed", "kelly"], default="fixed")
    r.add_argument("--kelly-fraction", type=float, default=0.25)
    r.add_argument("--bankroll", type=float, default=10_000.0, help="virtual bankroll per delay variant")
    r.add_argument("--max-premium", type=float, default=0.015, help="max price per share above the leader's VWAP")
    r.add_argument("--min-fill-ratio", type=float, default=1.0, help="share of the stake that must fill under the cap")
    r.add_argument("--window", type=float, default=1.5, help="seconds to collect an order's fills before acting")
    r.add_argument("--paper-db", default="data/paper.sqlite")
    r.add_argument("--alerts-log", default="data/alerts.log", help="dry-run alert sink when Telegram is not set up")
    r.add_argument("--dry-run", action="store_true", help="never send to Telegram, even if configured")
    r.add_argument("--no-exit-alerts", action="store_true")
    r.add_argument("--duration", type=float, default=0.0, help="seconds to run (0 = until interrupted)")
    r.add_argument(
        "--drain", type=float, help="seconds to wait for pending snapshots at the end (default max delay+15)"
    )
    r.add_argument("--positions-every", type=float, default=300.0, help="seconds between /positions polls")
    r.add_argument("--resolutions-every", type=float, default=600.0)
    r.add_argument("--mtm-every", type=float, default=900.0, help="seconds between mark-to-market equity records")
    dbg = r.add_argument_group("debug, for smoke tests only")
    dbg.add_argument(
        "--debug-watch-all-min-usdc",
        type=float,
        metavar="X",
        help="DEBUG: treat ANY wallet's BUY order of at least X USDC as a tracked signal (tier 'debug')",
    )
    dbg.add_argument("--debug-alert", action="store_true", help="DEBUG: also send alerts for debug signals")
    r.set_defaults(func=cmd_live_run)

    rep = sub.add_parser("report", help="paper-trading metrics by delay, wallet, category, strategy")
    rep.add_argument("--paper-db", default="data/paper.sqlite")
    rep.add_argument("--strategy", help="only lots with this strategy label")
    rep.add_argument("--n-boot", type=int, default=1000, help="bootstrap resamples for ROI confidence intervals")
    rep.add_argument("--min-signals", type=int, default=1, help="hide wallet rows with fewer signals")
    rep.add_argument("--out", help="also write the full report as JSON")
    rep.set_defaults(func=cmd_live_report)
    return live


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m sniper.live.cli", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    register(sub)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    asyncio.run(args.func(args, Settings(), None))


if __name__ == "__main__":
    main()
