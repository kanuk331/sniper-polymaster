"""Paper-trading report: rebuild `metrics.Lot`s from the paper DB and summarise them exactly like backtests."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np

from sniper.live.store import PaperStore, lot_from_json
from sniper.metrics import Lot, breakdown, fmt_ci

NOTES = [
    "Open positions are carried at cost (zero PnL) until an exit or resolution; see open_share and the MTM section.",
    "max_drawdown is realized-basis (metrics.capital_and_drawdown); mtm.max_drawdown uses periodic mid-price marks.",
    "Each latency_s is an independent paper portfolio with its own risk limits; skip reasons are in missed_by_reason.",
]


def lots_from_store(store: PaperStore, *, strategy: str | None = None) -> list[Lot]:
    """Lots as of now: open follower basis carried at cost, open benchmark shares at the leader's price."""
    out: list[Lot] = []
    for row in store.lot_rows(strategy):
        lot = lot_from_json(row["lot_json"])
        lot.open_cost = float(row["open_basis"]) if lot.filled else 0.0
        lot.bench_value += float(row["bench_shares"]) * lot.leader_price
        out.append(lot)
    return out


def _quantiles(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"n": 0, "p50": None, "p90": None}
    arr = np.asarray(values, dtype=float)
    return {"n": int(arr.size), "p50": float(np.quantile(arr, 0.5)), "p90": float(np.quantile(arr, 0.9))}


def mtm_drawdown(rows: Sequence[Any]) -> dict[str, dict[str, Any]]:
    series: dict[str, list[tuple[int, float, int]]] = defaultdict(list)
    for r in rows:
        series[r["variant"]].append((int(r["ts"]), float(r["equity"]), int(r["unmarked"] or 0)))
    out: dict[str, dict[str, Any]] = {}
    for variant, points in sorted(series.items()):
        points.sort()
        peak, max_dd, max_dd_pct = -math.inf, 0.0, 0.0
        for _, equity, _ in points:
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
            max_dd_pct = max(max_dd_pct, (peak - equity) / peak if peak > 0 else 0.0)
        out[variant] = {
            "points": len(points),
            "first_ts": points[0][0],
            "last_ts": points[-1][0],
            "last_equity": points[-1][1],
            "peak_equity": peak,
            "max_drawdown": max_dd,
            "max_drawdown_pct": max_dd_pct,
            "last_unmarked": points[-1][2],
        }
    return out


def execution_stats(store: PaperStore) -> dict[str, Any]:
    """Pipeline timing: detection lag, book fetch latency, scheduler lateness and effective delay per variant."""
    orders = store.rows("SELECT side, leader_ts, received_ms, detected_ms, feed_ts_ms, fee FROM orders")
    out: dict[str, Any] = {
        "orders": dict(Counter(r["side"] for r in orders)),
        "feed_lag_s": _quantiles([r["received_ms"] / 1000 - r["leader_ts"] for r in orders if r["leader_ts"]]),
        "detection_lag_s": _quantiles([r["detected_ms"] / 1000 - r["leader_ts"] for r in orders if r["leader_ts"]]),
        "leader_taker_share": (sum(1 for r in orders if (r["fee"] or 0) > 0) / len(orders)) if orders else None,
        "variants": {},
        "snapshots_by_purpose": dict(Counter(r["purpose"] for r in store.rows("SELECT purpose FROM snapshots"))),
    }
    rows = store.rows(
        "SELECT variant, req_start_ms, req_end_ms, scheduled_ms, leader_ts, book_ts_ms, error FROM snapshots "
        "WHERE purpose = 'entry'"
    )
    by_variant: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        by_variant[r["variant"]].append(r)
    for variant, group in sorted(by_variant.items(), key=lambda kv: (len(kv[0]), kv[0])):
        ok = [r for r in group if r["error"] is None]
        out["variants"][variant] = {
            "snapshots": len(group),
            "errors": len(group) - len(ok),
            "fetch_ms": _quantiles([r["req_end_ms"] - r["req_start_ms"] for r in group]),
            "start_lag_ms": _quantiles([r["req_start_ms"] - r["scheduled_ms"] for r in group if r["scheduled_ms"]]),
            "effective_delay_s": _quantiles([r["req_end_ms"] / 1000 - r["leader_ts"] for r in group if r["leader_ts"]]),
            # the book has not changed since before the leader's trade second: possibly a cached book
            "book_unchanged_since_trade": (
                sum(1 for r in ok if r["book_ts_ms"] and r["book_ts_ms"] < r["leader_ts"] * 1000) / len(ok)
                if ok
                else None
            ),
        }
    status = store.rows("SELECT variant, status, COUNT(*) AS n FROM lots GROUP BY variant, status")
    decisions: dict[str, dict[str, int]] = defaultdict(dict)
    for r in status:
        decisions[r["variant"]][r["status"]] = r["n"]
    out["decisions"] = dict(decisions)
    exits = store.rows("SELECT variant, kind, status, COUNT(*) AS n FROM exits GROUP BY variant, kind, status")
    out["exits"] = [dict(r) for r in exits]
    return out


def build_report(
    store: PaperStore, *, strategy: str | None = None, n_boot: int = 1000, min_signals: int = 1
) -> dict[str, Any]:
    lots = lots_from_store(store, strategy=strategy)
    by_variant = breakdown(lots, ["strategy", "latency_s"], n_boot=n_boot)
    for row in by_variant:
        row["variant"] = f"{row['strategy']}@{row['latency_s']}s"
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "paper_db": store.path,
        "strategy": strategy,
        "lots": len(lots),
        "by_latency": breakdown(lots, ["latency_s"], n_boot=n_boot),
        "by_strategy_latency": by_variant,
        "by_tier_latency": breakdown(lots, ["tier", "latency_s"], n_boot=n_boot),
        "by_category_latency": breakdown(lots, ["category", "latency_s"], n_boot=n_boot),
        "by_wallet_latency": breakdown(
            lots, ["wallet", "user_name", "tier", "latency_s"], n_boot=n_boot, min_signals=min_signals
        ),
        "mtm": mtm_drawdown(store.rows("SELECT ts, variant, equity, unmarked FROM equity")),
        "execution": execution_stats(store),
        "notes": NOTES,
    }


# --- text output ------------------------------------------------------------------------------------------------------

SUMMARY_COLS = [
    ("signals", "sig", "{:>5}"),
    ("filled", "fill", "{:>5}"),
    ("missed", "miss", "{:>5}"),
    ("cost", "cost$", "{:>9.0f}"),
    ("pnl", "pnl$", "{:>8.0f}"),
    ("roi", "roi", "{:>7.1%}"),
    ("leader_roi", "lead", "{:>7.1%}"),
    ("fees", "fees$", "{:>7.1f}"),
    ("slippage_cents", "slip¢", "{:>6.2f}"),
    ("peak_capital", "peak$", "{:>8.0f}"),
    ("max_drawdown", "dd$", "{:>7.0f}"),
    ("open_share", "open", "{:>5.0%}"),
]


def _table(rows: Sequence[dict[str, Any]], keys: Sequence[str], *, limit: int | None = None) -> list[str]:
    widths = {k: max([len(k), *(len(str(r[k])[:42]) for r in rows)]) for k in keys}
    head = " ".join(f"{k:<{widths[k]}}" for k in keys)
    head += " " + " ".join(f"{label:>{len(fmt.format(0)) if 'f' in fmt else 5}}" for _, label, fmt in SUMMARY_COLS)
    lines = [head + "  ci95             missed_by_reason"]
    for r in rows[:limit]:
        cells = " ".join(f"{str(r[k])[:42]:<{widths[k]}}" for k in keys)
        cells += " " + " ".join(fmt.format(r[col]) for col, _, fmt in SUMMARY_COLS)
        reasons = ",".join(f"{k}:{v}" for k, v in sorted(r["missed_by_reason"].items(), key=lambda kv: -kv[1]))
        lines.append(f"{cells}  {fmt_ci(r['roi_ci95']):<16} {reasons}")
    if limit is not None and len(rows) > limit:
        lines.append(f"... {len(rows) - limit} more rows in the JSON output")
    return lines


def _q(q: dict[str, Any], digits: int = 1) -> str:
    if not q or q.get("p50") is None:
        return "n/a"
    return f"{q['p50']:.{digits}f}/{q['p90']:.{digits}f}"


def format_report(report: dict[str, Any], *, wallet_rows: int = 30) -> str:
    lines = [f"paper report: {report['paper_db']} · {report['lots']} lots · {report['generated_at']}"]
    sections = [
        ("by latency", "by_latency", ["latency_s"], None),
        ("by strategy x latency", "by_strategy_latency", ["strategy", "latency_s"], None),
        ("by tier x latency", "by_tier_latency", ["tier", "latency_s"], None),
        ("by category x latency", "by_category_latency", ["category", "latency_s"], None),
        ("by wallet x latency", "by_wallet_latency", ["user_name", "wallet", "latency_s"], wallet_rows),
    ]
    for title, key, cols, limit in sections:
        rows = report.get(key) or []
        if not rows:
            continue
        if key == "by_wallet_latency":
            rows = sorted(rows, key=lambda r: (-r["signals"], r["wallet"], r["latency_s"]))
        lines += ["", f"== {title} =="] + _table(rows, cols, limit=limit)
    mtm = report.get("mtm") or {}
    if mtm:
        lines += ["", "== mark-to-market equity (mid prices) =="]
        for variant, m in mtm.items():
            lines.append(
                f"{variant:<18} points {m['points']:>4}  last {m['last_equity']:>10.2f}  peak {m['peak_equity']:>10.2f}"
                f"  max_dd {m['max_drawdown']:>8.2f} ({m['max_drawdown_pct']:.2%})  unmarked {m['last_unmarked']}"
            )
    ex = report.get("execution") or {}
    if ex:
        lines += [
            "",
            "== execution timing (p50/p90) ==",
            f"orders {ex.get('orders')}  feed lag {_q(ex.get('feed_lag_s'))} s  "
            f"detection lag {_q(ex.get('detection_lag_s'))} s  leader taker share {ex.get('leader_taker_share')}",
            f"snapshots by purpose {ex.get('snapshots_by_purpose')}",
        ]
        for variant, v in (ex.get("variants") or {}).items():
            unchanged = v["book_unchanged_since_trade"]
            lines.append(
                f"{variant:<18} snaps {v['snapshots']:>5} err {v['errors']:>3}  fetch {_q(v['fetch_ms'], 0)} ms"
                f"  start lag {_q(v['start_lag_ms'], 0)} ms  effective delay {_q(v['effective_delay_s'])} s"
                f"  book unchanged since trade {'n/a' if unchanged is None else f'{unchanged:.0%}'}"
            )
        for variant, counts in sorted((ex.get("decisions") or {}).items()):
            lines.append(f"{variant:<18} decisions {dict(sorted(counts.items(), key=lambda kv: -kv[1]))}")
    lines += ["", *[f"note: {n}" for n in report.get("notes") or []]]
    return "\n".join(lines)
