"""Why does the phase-1 candidate group earn +13% at 10 s latency but +24% at 60 s? (research iteration 2, item 1)

Re-simulates the five phase-1 candidates (split 2026-09-01, gap 7 d, horizon 45 d) with the ledger-based simulator,
checks the per-wallet PnL against the phase-1 v3 report, then diffs every signal between the two latencies and pulls
the second-resolution trade tape around the signals that explain the gap.

Usage: SNIPER_DB_PATH=data/live.sqlite uv run python research/latency_discrepancy.py --out reports/latency_discrepancy.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sniper import backtest, walkforward
from sniper.analytics import load_meta
from sniper.config import Settings
from sniper.db import Database
from sniper.polymarket import DataApi
from sniper.scoring import score_wallets

SPLIT = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp())
END = SPLIT + 45 * 86400
LATENCIES = (10, 60)


def utc(ts: int | None) -> str:
    return "" if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S")


async def tape(api: DataApi, condition_id: str, ts: int, before: int = 60, after: int = 240, max_pages: int = 60):
    """Market trades in [ts - before, ts + after], paging back from the newest trade (the feed is newest-first)."""
    rows, pages = [], 0
    params = {"condition": condition_id, "taker_only": False, "limit": 1000}
    async for t in api.client.paginate("/trades", params):
        if t["timestamp"] < ts - before:
            break
        if t["timestamp"] <= ts + after:
            rows.append(t)
        pages += 1
        if pages > max_pages * 1000:
            return None
    return sorted(rows, key=lambda t: t["timestamp"])


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase1-report", default="reports/wf-2026-09-v3.json")
    ap.add_argument("--out", default="reports/latency_discrepancy.json")
    ap.add_argument("--tape-top", type=int, default=0, help="fetch the trade tape around the top N signals")
    args = ap.parse_args()

    settings = Settings()
    db = Database(settings.db_path)
    v3 = json.load(open(args.phase1_report))
    v3_rows = [w for w in v3["wallets"] if w["tier"] == "candidate"]
    wallets = sorted({w["wallet"] for w in v3_rows})
    v3_pnl = {(w["wallet"], w["copy"]["latency_s"]): w["copy"]["pnl"] for w in v3_rows}
    meta = load_meta(db)
    scores = {s.wallet: s for s in score_wallets(db, wallets, as_of=SPLIT - 7 * 86400, meta=meta)}
    book = await walkforward.price_book_for(db, settings, list(scores.values()), SPLIT, END, meta, pad_after=1500)

    lots = {}
    regression = []
    for w in wallets:
        rows = db.activity(w, until=END)
        for lat in LATENCIES:
            params = backtest.CopyParams(latency_s=lat)
            labels = backtest.LotLabels(period="p2026-09-phase1", user_name=scores[w].user_name or "", tier="candidate")
            out = backtest.simulate_copy(rows, meta, book, start=SPLIT, end=END, params=params, wallet=w, labels=labels)
            lots[(w, lat)] = out
            regression.append(
                {"wallet": w, "user": scores[w].user_name, "latency_s": lat,
                 "pnl_new": round(sum(x.pnl for x in out), 2), "pnl_v3": round(v3_pnl.get((w, lat), float("nan")), 2)}
            )  # fmt: skip

    # signal-level diff between latencies
    diffs = []
    for w in wallets:
        # Both runs see the same signals in the same order (the signal cap does not depend on latency).
        assert [(x.signal_ts, x.token_id) for x in lots[(w, 10)]] == [(y.signal_ts, y.token_id) for y in lots[(w, 60)]]
        for x, y in zip(lots[(w, 10)], lots[(w, 60)], strict=True):
            kind = ("F" if x.filled else "S") + ("F" if y.filled else "S")
            diffs.append((w, (x.signal_ts, x.token_id), kind, x, y, y.pnl - x.pnl))

    by_class = defaultdict(lambda: {"n": 0, "dpnl": 0.0})
    by_wallet = defaultdict(float)
    for w, _, kind, x, y, d in diffs:
        by_class[kind]["n"] += 1
        by_class[kind]["dpnl"] += d
        by_wallet[scores[w].user_name] += d

    eff = {
        lat: [x.exec_ts - x.signal_ts for (w, la), ls in lots.items() if la == lat for x in ls if x.exec_ts]
        for lat in LATENCIES
    }
    tokens = db.tokens()
    top = sorted(diffs, key=lambda d: -abs(d[5]))[:15]
    top_rows = []
    async with DataApi.create(settings).client as client:
        api = DataApi(client)
        for i, (w, (ts, token), kind, x, y, d) in enumerate(top):
            t = tokens.get(token)
            row = {
                "user": scores[w].user_name, "signal_utc": utc(ts), "market": t["market_slug"] if t else token[:12],
                "outcome": t["outcome"] if t else "", "payout": None if not t or t["final_price"] is None else t["final_price"],
                "leader_vwap": round(x.leader_price, 4), "leader_worst": round(x.leader_worst, 4),
                "leader_notional": round(x.leader_notional, 0),
                "l10": {"status": x.status, "bar_offset_s": None if not x.exec_ts else x.exec_ts - ts, "bar_price": x.ref_price,
                        "exec_price": x.exec_price, "exit_proceeds": round(x.exit_proceeds, 2), "pnl": round(x.pnl, 2)},
                "l60": {"status": y.status, "bar_offset_s": None if not y.exec_ts else y.exec_ts - ts, "bar_price": y.ref_price,
                        "exec_price": y.exec_price, "exit_proceeds": round(y.exit_proceeds, 2), "pnl": round(y.pnl, 2)},
                "dpnl": round(d, 2), "class": kind,
            }  # fmt: skip
            if i < args.tape_top and t:
                trades = await tape(api, t["condition_id"], ts)
                if trades is not None:
                    same = [tr for tr in trades if tr["token_id"] == token]
                    row["tape"] = [
                        {"dt": tr["timestamp"] - ts, "side": tr["side"], "price": tr["price"], "size": round(tr["size"], 1),
                         "is_leader": tr["proxy_wallet"].lower() == w}
                        for tr in same
                    ]  # fmt: skip
            top_rows.append(row)

    from sniper.metrics import summarize

    per_market = defaultdict(lambda: {10: 0.0, 60: 0.0})
    for (w, la), ls in lots.items():
        for x in ls:
            t = tokens.get(x.token_id)
            per_market[(scores[w].user_name, t["market_slug"] if t else x.token_id[:12])][la] += x.pnl
    top_markets = sorted(per_market.items(), key=lambda kv: -abs(kv[1][60] - kv[1][10]))[:8]
    all10 = [x for (w, la), ls in lots.items() if la == 10 for x in ls]
    all60 = [x for (w, la), ls in lots.items() if la == 60 for x in ls]
    worst_event = max(per_market.items(), key=lambda kv: kv[1][60] - kv[1][10])[0][1]

    def ex(ls):
        return [x for x in ls if not (tokens.get(x.token_id) and tokens[x.token_id]["market_slug"] == worst_event)]

    summaries = {
        "all": {lat: summarize(ls, n_boot=2000) for lat, ls in ((10, all10), (60, all60))},
        f"without {worst_event}": {lat: summarize(ex(ls), n_boot=2000) for lat, ls in ((10, all10), (60, all60))},
    }
    total_10 = sum(x.pnl for (w, la), ls in lots.items() if la == 10 for x in ls)
    total_60 = sum(x.pnl for (w, la), ls in lots.items() if la == 60 for x in ls)
    report = {
        "regression_vs_phase1_v3": regression,
        "totals": {"pnl_10": round(total_10, 2), "pnl_60": round(total_60, 2), "delta": round(total_60 - total_10, 2)},
        "delta_by_wallet": {k: round(v, 2) for k, v in sorted(by_wallet.items(), key=lambda kv: -abs(kv[1]))},
        "delta_by_transition": {k: {"n": v["n"], "dpnl": round(v["dpnl"], 2)} for k, v in sorted(by_class.items())},
        "effective_delay_s": {
            lat: {
                "p10": sorted(v)[len(v) // 10],
                "median": statistics.median(v),
                "p90": sorted(v)[9 * len(v) // 10],
                "n": len(v),
            }
            for lat, v in eff.items()
            if v
        },  # fmt: skip
        "skip_reasons": {
            lat: dict(Counter(x.status for (w, la), ls in lots.items() if la == lat for x in ls)) for lat in LATENCIES
        },
        "top_signals": top_rows,
        "by_market": [
            {"user": u, "market": m, "pnl_10": round(v[10], 2), "pnl_60": round(v[60], 2)} for (u, m), v in top_markets
        ],
        "summaries": {
            k: {
                lat: {
                    f: v[f]
                    for f in (
                        "filled",
                        "cost",
                        "pnl",
                        "roi",
                        "roi_ci95",
                        "fees",
                        "slippage_cents",
                        "missed_share",
                        "peak_capital",
                        "max_drawdown",
                    )
                }
                for lat, v in d.items()
            }
            for k, d in summaries.items()
        },
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: report[k] for k in ("regression_vs_phase1_v3", "totals", "delta_by_wallet", "delta_by_transition",
                                              "effective_delay_s", "skip_reasons")}, indent=1, default=str))  # fmt: skip


if __name__ == "__main__":
    asyncio.run(main())
