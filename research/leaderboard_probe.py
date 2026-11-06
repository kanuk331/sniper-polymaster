"""Probe: how many top-PnL Polymarket wallets are plausible copy-trade candidates?

Pulls the PnL leaderboard from Data API v2 and tags each wallet with heuristic
archetypes that make naive copy-trading unsafe (short-term crypto bots, market
makers / arbitrageurs, near-certainty grinders, one-bet wonders, thin samples).
Read-only public endpoints, no auth. Heuristics are deliberately simple; this
is a sanity check of the idea, not the production scoring model.

Usage: python3 research/leaderboard_probe.py [--top 100] [--period month] [--out /tmp/probe.json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

BASE = "https://data-api.polymarket.com/v2"
POSITION_STATUSES = ("OPEN", "CLOSED", "REDEEMABLE", "REDEEMABLE_LOST")
SHORT_CRYPTO_MARKERS = ("updown", "up-or-down")
PNL_FIELDS = ("economic_pnl", "position_pnl", "wallet_income", "fees_paid", "volume_usdc", "trade_count")


def get(path: str, **params) -> dict:
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "sniper-polymaster-research/0.1"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise
            time.sleep(float(exc.headers.get("Retry-After") or 2**attempt))
        except urllib.error.URLError:
            time.sleep(2**attempt)
    raise RuntimeError(f"giving up on {url}")


def delta(points: list[dict], field: str) -> float:
    if not points:
        return 0.0
    return float(points[-1].get(field) or 0.0) - float(points[0].get(field) or 0.0)


def profile(entry: dict, window_start: int) -> dict:
    wallet = entry["user_id"]
    # /v2/trades defaults to taker_only=true, which hides maker fills (market makers).
    trades = get("/trades", user=wallet, limit=1000, taker_only="false")["data"]
    positions, truncated = [], False
    for status in POSITION_STATUSES:
        page = get("/positions", user=wallet, status=status, start=window_start, sort_by="TOTAL_PNL", limit=1000)
        positions += page["data"]
        truncated |= bool((page.get("pagination") or {}).get("has_more"))
    points = (get("/user-pnl", user=wallet, interval="1m", fidelity="1d")["data"] or {}).get("points") or []

    buys = [t for t in trades if t["side"] == "BUY"]
    buy_notional = sum(t["price"] * t["size"] for t in buys) or 1e-9
    sides_by_market: dict[str, set] = defaultdict(set)
    for t in buys:
        sides_by_market[t["condition_id"]].add(t["outcome_index"])
    timestamps = [t["timestamp"] for t in trades]
    pos_pnls = [float(p.get("total_pnl") or 0.0) for p in positions]
    pos_sum = sum(pos_pnls)
    d = {f: delta(points, f) for f in PNL_FIELDS}

    m = {
        "user_name": entry.get("user_name"),
        "rank": entry["rank"],
        "leaderboard_pnl": entry["pnl"],
        "leaderboard_volume": entry["volume"],
        "trades_sampled": len(trades),
        "trades_per_day": len(trades) / max((max(timestamps) - min(timestamps)) / 86400, 1 / 24)
        if len(trades) > 1
        else 0.0,
        "short_crypto_share": sum(any(k in (t.get("slug") or "") for k in SHORT_CRYPTO_MARKERS) for t in trades)
        / max(len(trades), 1),
        "sell_share": 1 - len(buys) / max(len(trades), 1),
        "high_price_buy_share": sum(t["price"] * t["size"] for t in buys if t["price"] >= 0.95) / buy_notional,
        # Buying both outcomes of the same market is a market-making / arbitrage signature.
        "two_sided_share": sum(len(s) > 1 for s in sides_by_market.values()) / max(len(sides_by_market), 1),
        "markets_in_window": len({p["condition_id"] for p in positions}),
        "positions_truncated": truncated,
        "top1_share": (max(pos_pnls) / pos_sum) if pos_pnls and pos_sum > 0 else None,
        "month_economic_pnl": d["economic_pnl"],
        "month_wallet_income": d["wallet_income"],
        "income_share": (d["wallet_income"] / d["economic_pnl"]) if d["economic_pnl"] > 0 else None,
        "month_fees_paid": d["fees_paid"],
    }
    flags = []
    if m["short_crypto_share"] >= 0.5:
        flags.append("short_crypto_bot")
    if m["two_sided_share"] >= 0.3 or (m["income_share"] is not None and m["income_share"] >= 0.25):
        flags.append("mm_or_arb")
    if m["high_price_buy_share"] >= 0.5:
        flags.append("near_certainty_grinder")
    if m["top1_share"] is not None and m["top1_share"] >= 0.5:
        flags.append("one_bet_concentration")
    if m["markets_in_window"] < 20:
        flags.append("thin_sample")
    if m["month_economic_pnl"] <= 0:
        flags.append("not_profitable_by_economic_pnl")
    m["flags"] = flags
    return m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--period", default="month", choices=["day", "week", "month", "all"])
    ap.add_argument("--out", default="/tmp/leaderboard_probe.json")
    args = ap.parse_args()

    started = time.time()
    board = get("/leaderboard", time_period=args.period, sort_by="PNL", limit=args.top)["data"]
    window_start = int(started) - 30 * 86400
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda e: profile(e, window_start), board))

    flag_counts = Counter(f for r in rows for f in r["flags"])
    candidates = [r for r in rows if not r["flags"]]
    ratios = [r["month_economic_pnl"] / r["leaderboard_pnl"] for r in rows if r["leaderboard_pnl"]]
    summary = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "period": args.period,
        "wallets": len(rows),
        "flag_counts": dict(flag_counts.most_common()),
        "clean_candidates": len(candidates),
        "candidate_names": [r["user_name"] for r in candidates],
        "median_economic_pnl_to_leaderboard_pnl": statistics.median(ratios) if ratios else None,
        "wallets_with_economic_pnl_below_half_of_leaderboard": sum(x < 0.5 for x in ratios),
        "positions_truncated_wallets": sum(r["positions_truncated"] for r in rows),
        "elapsed_s": round(time.time() - started, 1),
    }
    with open(args.out, "w") as fh:
        json.dump({"summary": summary, "rows": rows}, fh, indent=1)
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
