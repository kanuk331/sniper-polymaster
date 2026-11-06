"""Multi-period walk-forward: data collection, evaluation and cross-period reporting."""

from __future__ import annotations

import csv
import gzip
import json
import logging
import subprocess
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from sniper import backtest, ingest, metrics
from sniper.analytics import Meta, load_meta
from sniper.config import Settings
from sniper.copyability import FastFilter, assess, plan_sample
from sniper.db import Database
from sniper.metrics import Lot
from sniper.plan import Period
from sniper.polymarket import ClobApi, DataApi, GammaApi
from sniper.scoring import Thresholds, WalletScore, apply_flags, score_row, score_wallets, tier_summary, write_scores

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
    rows = [r | {"flows": json.dumps(lot.flows)} for lot, r in ((x, x.as_row()) for x in lots)]
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


def read_lots(path: Path) -> list[Lot]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    ints = {"latency_s", "signal_ts", "exec_ts", "close_ts"}
    strs = {"period", "strategy", "wallet", "user_name", "tier", "style", "category", "event_key", "condition_id",
            "token_id", "status"}  # fmt: skip
    lots = []
    with gzip.open(path, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            parsed: dict[str, Any] = {}
            for k, v in row.items():
                if k == "flows":
                    parsed[k] = [tuple(x) for x in json.loads(v)] if v else []
                elif k in strs:
                    parsed[k] = v
                elif v in ("", "None"):
                    parsed[k] = None
                elif k in ints:
                    parsed[k] = int(float(v))
                else:
                    parsed[k] = float(v)
            lots.append(metrics.lot_from_row(parsed))
    return lots


# --- evaluation ------------------------------------------------------------------------------------------------


def git_state() -> tuple[str, bool]:
    """(HEAD commit, whether src/ or config/ have uncommitted changes)."""
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "src", "config"], capture_output=True, text=True)
    return head, bool(dirty.stdout.strip())


def holdout_guard(period: Period, out_dir: Path, *, confirm: bool, allow_rerun: bool) -> str:
    commit, dirty = git_state()
    if period.role != "holdout":
        return commit
    if not confirm:
        raise SystemExit(f"{period.name} is a holdout period: pass --confirm-holdout to evaluate it (once)")
    if dirty:
        raise SystemExit("holdout evaluation needs frozen code: commit src/ and config/ first")
    marker = out_dir / "HOLDOUT_EVALUATED"
    if marker.exists() and not allow_rerun:
        raise SystemExit(f"{period.name} was already evaluated ({marker.read_text().strip()}); refusing to reuse it")
    return commit


async def evaluate(
    db: Database,
    period: Period,
    settings: Settings,
    out_root: Path,
    *,
    strategies: Sequence[backtest.CopyParams],
    latencies: Sequence[int],
    ff: FastFilter,
    thresholds: Thresholds | None = None,
    sampling: dict[str, int] | None = None,
    confirm_holdout: bool = False,
    allow_holdout_rerun: bool = False,
    n_boot: int = 1000,
) -> dict[str, Any]:
    t0 = time.time()
    out = period_dir(out_root, period)
    commit = holdout_guard(period, out, confirm=confirm_holdout, allow_rerun=allow_holdout_rerun)
    primary = strategies[0]
    wallets = db.universe(period.snapshot)
    start, end = period.window
    status = {w: db.window_status(w, start, end) for w in wallets}
    complete = sorted(w for w, st in status.items() if st == "complete")
    log.info("%s: %d wallets, status %s", period.name, len(wallets), dict(Counter(status.values())))

    meta = load_meta(db, wallets=complete, start=start, end=end)
    scores = score_wallets(db, complete, as_of=period.as_of, thresholds=thresholds, meta=meta, since=start)

    # Fast-trade filter, only where it can change the outcome (wallets not already excluded or thin).
    eligible = [s for s in scores if s.tier in ("candidate", "watch", "noise")]
    pre_rows = {s.wallet: db.activity(s.wallet, since=start, until=period.as_of) for s in eligible}
    samples = {
        s.wallet: plan_sample(pre_rows[s.wallet], meta, as_of=period.as_of, start=start, strategy=primary, ff=ff)
        for s in eligible
    }
    windows = ingest.merge_windows(
        [p for smp in samples.values() for p in smp.points], pad_after=max(ff.latencies) + 600, join_gap=3600
    )
    clob = ClobApi.create(settings)
    try:
        await ingest.fetch_price_windows(db, clob, windows)
    finally:
        await clob.aclose()
    pre_book = backtest.PriceBook(
        {t: db.prices(t, min(a for a, _ in ws) - 120, max(b for _, b in ws)) for t, ws in windows.items()}
    )
    copy = {
        w: assess(pre_rows[w], meta, pre_book, smp, wallet=w, as_of=period.as_of, strategy=primary, ff=ff)
        for w, smp in samples.items()
    }
    apply_flags(scores, {w: c.flags for w, c in copy.items()}, thresholds)
    for s in scores:
        s.copyability = copy.get(s.wallet)

    chosen = backtest.choose_wallets_to_simulate(scores, **(sampling or {}))
    book = await price_book_for(
        db, settings, chosen, period.split_ts, period.eval_end, meta, pad_after=max(latencies) + 600
    )
    lots = []
    for s in chosen:
        rows = db.activity(s.wallet, since=start, until=period.eval_end)
        labels = backtest.LotLabels(period=period.name, user_name=s.user_name or "", tier=s.tier, style=s.style)
        for strategy in strategies:
            for lat in latencies:
                params = backtest.CopyParams(**(strategy.__dict__ | {"latency_s": lat}))
                lots += backtest.simulate_copy(
                    rows, meta, book, start=period.split_ts, end=period.eval_end, params=params, wallet=s.wallet,
                    labels=labels,
                )  # fmt: skip

    write_scores(scores, out / "scores.csv")
    write_csv(out / "copyability.csv", [c.as_row() for c in copy.values()])
    write_lots(lots, out / "lots.csv.gz")
    lead = [s for s in scores if s.tier in ("candidate", "watch")]
    lead_set = {s.wallet for s in lead}
    summary = {
        "period": period.name,
        "role": period.role,
        "conditions": period.conditions,
        "commit": commit,
        "split": period.split,
        "as_of": period.as_of,
        "eval_end": period.eval_end,
        "strategies": [s.__dict__ for s in strategies],
        "latencies": list(latencies),
        "fast_filter": ff.__dict__,
        "universe": {"wallets": len(wallets), "status": dict(Counter(status.values()))},
        "score_tiers": tier_summary(scores),
        "copyability_flags": dict(Counter(f for c in copy.values() for f in c.flags)),
        "simulated_wallets": dict(Counter(s.tier for s in chosen)),
        "leads": [score_row(s) for s in lead],
        "by_tier": metrics.breakdown(lots, ["strategy", "tier", "latency_s"], n_boot=n_boot),
        "by_style": metrics.breakdown(lots, ["strategy", "style", "latency_s"], n_boot=n_boot),
        "by_category_leads": metrics.breakdown(
            [x for x in lots if x.wallet in lead_set], ["strategy", "category", "latency_s"], n_boot=n_boot
        ),
        "by_wallet_leads": metrics.breakdown(
            [x for x in lots if x.wallet in lead_set],
            ["strategy", "wallet", "user_name", "tier", "latency_s"],
            n_boot=n_boot,
        ),
        "predictive": [
            backtest.predictive_check([x for x in lots if x.strategy == st.strategy], scores, lat)
            | {"strategy": st.strategy}
            for st in strategies
            for lat in latencies
        ],
        "elapsed_s": round(time.time() - t0, 1),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    if period.role == "holdout":
        (out / "HOLDOUT_EVALUATED").write_text(
            f"commit {commit} at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        )
    log.info("%s evaluated in %.0fs", period.name, time.time() - t0)
    return summary


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


# --- cross-period report -------------------------------------------------------------------------------------------

LEAD_TIERS = ("candidate", "watch")


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1%}"


def _md_table(rows: Sequence[dict[str, Any]], cols: Sequence[tuple[str, str]]) -> str:
    head = "| " + " | ".join(title for _, title in cols) + " |\n|" + "---|" * len(cols) + "\n"
    body = ""
    for r in rows:
        cells = []
        for key, _ in cols:
            v = r.get(key)
            if key in ("roi", "leader_roi", "missed_share", "max_drawdown_pct_capital", "open_share"):
                cells.append(_pct(v))
            elif key == "roi_ci95":
                cells.append(metrics.fmt_ci(v))
            elif isinstance(v, float):
                cells.append(f"{v:,.2f}" if abs(v) < 100 else f"{v:,.0f}")
            elif isinstance(v, dict):
                cells.append(", ".join(f"{k} {n}" for k, n in sorted(v.items(), key=lambda kv: -kv[1])))
            else:
                cells.append("" if v is None else str(v))
        body += "| " + " | ".join(cells) + " |\n"
    return head + body


METRIC_COLS = [
    ("signals", "сигналов"), ("filled", "сделок"), ("missed_share", "пропущено"), ("cost", "вложено $"),
    ("fees", "комиссии $"), ("slippage_cents", "проскальз. ¢/акц"), ("pnl", "PnL $"), ("roi", "ROI"),
    ("roi_ci95", "95% ДИ ROI"), ("leader_roi", "ROI лидера"), ("peak_capital", "пик капитала $"),
    ("max_drawdown", "макс. просадка $"), ("open_share", "открыто"),
]  # fmt: skip


def report(
    periods: Sequence[Period], out_root: Path, *, label: str, primary: str, decision_latency: int, n_boot: int = 1000
) -> dict[str, Any]:
    loaded = []
    for p in periods:
        d = out_root / p.name
        if (d / "summary.json").exists():
            loaded.append((p, json.loads((d / "summary.json").read_text()), read_lots(d / "lots.csv.gz")))
    if not loaded:
        raise SystemExit("no evaluated periods found")
    lots = [x for _, _, ls in loaded for x in ls]
    lead_lots = [x for x in lots if x.tier in LEAD_TIERS]

    overview = [
        {"period": p.name, "role": p.role, "conditions": p.conditions, "universe": s["universe"]["wallets"],
         "complete": s["universe"]["status"].get("complete", 0), "capped": s["universe"]["status"].get("capped", 0),
         "tiers": s["score_tiers"]["tiers"], "copy_flags": s["copyability_flags"], "commit": s["commit"][:8]}
        for p, s, _ in loaded
    ]  # fmt: skip
    by_tier = metrics.breakdown(lots, ["strategy", "tier", "latency_s"], n_boot=n_boot)
    per_period = []
    for p, _, ls in loaded:
        for tier in ("candidate", "watch", "noise"):
            group = [x for x in ls if x.strategy == primary and x.tier == tier and x.latency_s == decision_latency]
            if group:
                per_period.append({"period": p.name, "tier": tier} | metrics.summarize(group, n_boot=n_boot))
    by_style = metrics.breakdown(
        [x for x in lots if x.latency_s == decision_latency], ["strategy", "style"], n_boot=n_boot
    )
    by_category = metrics.breakdown(
        [x for x in lead_lots if x.latency_s == decision_latency], ["strategy", "category"], n_boot=n_boot
    )
    by_latency_leads = metrics.breakdown(lead_lots, ["strategy", "latency_s"], n_boot=n_boot)

    traders = []
    for wallet in sorted({x.wallet for x in lead_lots}):
        mine = [
            x for x in lead_lots if x.wallet == wallet and x.strategy == primary and x.latency_s == decision_latency
        ]
        if not mine:
            continue
        per = {}
        for p, _, _ in loaded:
            g = [x for x in mine if x.period == p.name]
            if g:
                s = metrics.summarize(g, n_boot=200)
                per[p.name] = f"{s['roi']:+.1%} ({s['filled']})"
        s = metrics.summarize(mine, n_boot=n_boot)
        traders.append({"wallet": wallet, "user_name": mine[0].user_name, "periods_as_lead": len(per),
                        "per_period": per} | s)  # fmt: skip
    traders.sort(key=lambda r: (-r["periods_as_lead"], -(r["roi_ci95"] or (-9, -9))[0]))

    cand = [x for x in lots if x.strategy == primary and x.tier == "candidate" and x.latency_s == decision_latency]
    noise = [x for x in lots if x.strategy == primary and x.tier == "noise" and x.latency_s == decision_latency]
    cand_s = metrics.summarize(cand, n_boot=n_boot) if cand else None
    noise_s = metrics.summarize(noise, n_boot=n_boot) if noise else None
    per_cand = [r for r in per_period if r["tier"] == "candidate"]
    decision = {
        "primary_strategy": primary,
        "decision_latency_s": decision_latency,
        "candidate_pooled": cand_s,
        "noise_pooled": noise_s,
        "periods_with_candidates": len(per_cand),
        "periods_candidate_roi_positive": sum(r["roi"] > 0 for r in per_cand),
        "supported": bool(cand_s and cand_s["roi_ci95"] and cand_s["roi_ci95"][0] > 0),
        "repeatable_traders": [
            t["user_name"] or t["wallet"] for t in traders
            if t["periods_as_lead"] >= 2 and t["roi_ci95"] and t["roi_ci95"][0] > 0
        ],
    }  # fmt: skip
    result = {"label": label, "periods": [p.name for p, _, _ in loaded], "overview": overview, "decision": decision,
              "by_tier": by_tier, "per_period": per_period, "by_style": by_style, "by_category_leads": by_category,
              "by_latency_leads": by_latency_leads, "traders": traders,
              "predictive": {p.name: s["predictive"] for p, s, _ in loaded}}  # fmt: skip
    out = out_root / f"report-{label}.json"
    out.write_text(json.dumps(result, indent=1, default=str))
    (out_root / f"report-{label}.md").write_text(render_markdown(result, primary, decision_latency))
    return result


def render_markdown(r: dict[str, Any], primary: str, lat: int) -> str:
    d = r["decision"]
    c = d["candidate_pooled"]
    lines = [
        f"# Walk-forward: {r['label']} ({', '.join(r['periods'])})",
        "",
        f"Основная стратегия `{primary}`, задержка {lat} с. Решение: ROI candidate (пул по периодам) — "
        + (f"{_pct(c['roi'])}, 95% ДИ {metrics.fmt_ci(c['roi_ci95'])}, сделок {c['filled']}" if c else "нет candidate")
        + f"; ROI > 0 в {d['periods_candidate_roi_positive']} из {d['periods_with_candidates']} периодов с candidate;"
        + f" статистически подтверждено: **{'да' if d['supported'] else 'нет'}**.",
        "",
        "## Периоды",
        _md_table(r["overview"], [("period", "период"), ("role", "роль"), ("conditions", "условия"),
                                  ("universe", "кошельков"), ("complete", "полные"), ("capped", "лимит строк"),
                                  ("tiers", "тиры"), ("copy_flags", "флаги копируемости"), ("commit", "коммит")]),
        "## По тирам, стратегиям и задержкам (пул)",
        _md_table(
            r["by_tier"], [("strategy", "стратегия"), ("tier", "тир"), ("latency_s", "задержка с"), *METRIC_COLS]
        ),
        f"## По периодам ({primary}, {lat} с)",
        _md_table(r["per_period"], [("period", "период"), ("tier", "тир"), *METRIC_COLS]),
        "## candidate + watch по задержкам (пул)",
        _md_table(r["by_latency_leads"], [("strategy", "стратегия"), ("latency_s", "задержка с"), *METRIC_COLS]),
        f"## По стилю трейдера ({lat} с, пул)",
        _md_table(r["by_style"], [("strategy", "стратегия"), ("style", "стиль"), *METRIC_COLS]),
        f"## candidate + watch по категориям рынков ({lat} с, пул)",
        _md_table(r["by_category_leads"], [("strategy", "стратегия"), ("category", "категория"), *METRIC_COLS]),
        f"## Трейдеры из candidate/watch ({primary}, {lat} с)",
        _md_table(
            r["traders"],
            [("user_name", "трейдер"), ("periods_as_lead", "периодов"), ("per_period", "ROI по периодам (сделок)"),
             *METRIC_COLS],
        ),
    ]  # fmt: skip
    return "\n".join(lines) + "\n"
