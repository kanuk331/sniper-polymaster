"""Wallet scoring: archetype filters -> skill test -> multiple-testing control -> shrinkage -> tier."""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sniper.analytics import (
    Features,
    Meta,
    SkillStats,
    bh_qvalues,
    build_bets,
    empirical_bayes,
    features,
    load_meta,
    skill_stats,
)

Z_20 = 0.8416  # standard-normal 80th percentile, used for the conservative 20th-percentile edge


@dataclass(frozen=True)
class Thresholds:
    """Defaults from docs/research/2026-11-06-polymarket-sniper.md (sections 4-5); tune on data."""

    max_trades_per_active_day: float = 100.0
    max_short_crypto_share: float = 0.5
    max_two_sided_share: float = 0.3
    max_merge_conversion_share: float = 0.3
    max_income_share: float = 0.25
    max_high_price_buy_share: float = 0.5
    max_median_entry: float = 0.93
    max_top_event_share: float = 0.5
    min_events: int = 30
    min_history_days: float = 90.0
    q_watch: float = 0.10
    q_candidate: float = 0.05


HARD_FLAGS = ("hft_bot", "short_crypto", "mm_or_arb", "near_certainty_grinder", "concentrated")
TIERS = ("candidate", "watch", "noise", "insufficient", "excluded")


def archetype_flags(f: Features, t: Thresholds) -> list[str]:
    flags = []
    if f.trades_per_active_day > t.max_trades_per_active_day:
        flags.append("hft_bot")
    if f.short_crypto_share >= t.max_short_crypto_share:
        flags.append("short_crypto")
    if (
        f.two_sided_share >= t.max_two_sided_share
        or f.merge_conversion_share >= t.max_merge_conversion_share
        or f.income_share >= t.max_income_share
    ):
        flags.append("mm_or_arb")
    if f.high_price_buy_share >= t.max_high_price_buy_share or f.median_entry >= t.max_median_entry:
        flags.append("near_certainty_grinder")
    if f.resolved_pnl > 0 and (f.top_event_share >= t.max_top_event_share or f.pnl_ex_top3 <= 0):
        flags.append("concentrated")
    if f.n_events_resolved < t.min_events or f.history_days < t.min_history_days:
        flags.append("thin_sample")
    return flags


@dataclass
class WalletScore:
    wallet: str
    user_name: str | None
    features: Features
    skill: SkillStats
    flags: list[str]
    q_bh: float = 1.0
    q_by: float = 1.0
    theta_shrunk: float = 0.0
    theta_post_sd: float = math.inf
    theta_q20: float = -math.inf
    tier: str = "noise"
    notes: list[str] = field(default_factory=list)


def assign_tier(s: WalletScore, t: Thresholds) -> str:
    if any(f in HARD_FLAGS for f in s.flags):
        return "excluded"
    if "thin_sample" in s.flags:
        return "insufficient"
    if s.q_bh <= t.q_candidate and s.theta_q20 > 0:
        return "candidate"
    if s.q_bh <= t.q_watch and s.theta_shrunk > 0:
        return "watch"
    return "noise"


def score_wallets(
    db: Any,
    wallets: list[str],
    *,
    as_of: int | None = None,
    thresholds: Thresholds | None = None,
    meta: Meta | None = None,
) -> list[WalletScore]:
    """Score wallets using only information available strictly before `as_of` (None = now)."""
    t = thresholds or Thresholds()
    meta = meta or load_meta(db)
    names = db.wallet_names()
    scores: list[WalletScore] = []
    for wallet in wallets:
        rows = db.activity(wallet, until=as_of)
        bets = build_bets(rows, meta, as_of=as_of)
        feats = features(rows, meta, bets, as_of=as_of)
        stats = skill_stats(bets)
        scores.append(WalletScore(wallet, names.get(wallet), feats, stats, archetype_flags(feats, t)))

    # Every wallet we looked at belongs to the family of tests, including excluded ones.
    tested = [s for s in scores if s.skill.n_resolved > 0]
    pvals = [s.skill.p_value for s in tested]
    for s, q, q_dep in zip(tested, bh_qvalues(pvals), bh_qvalues(pvals, dependent=True), strict=True):
        s.q_bh, s.q_by = q, q_dep
    shrunk = empirical_bayes([s.skill.theta for s in tested], [s.skill.theta_se for s in tested])
    for s, sh in zip(tested, shrunk, strict=True):
        s.theta_shrunk, s.theta_post_sd = sh.mean, sh.sd
        s.theta_q20 = sh.mean - Z_20 * sh.sd
    for s in scores:
        s.tier = assign_tier(s, t)
    scores.sort(key=lambda s: (TIERS.index(s.tier), -s.theta_q20 if math.isfinite(s.theta_q20) else math.inf))
    return scores


# --- output ------------------------------------------------------------------------------------------------------

CSV_FIELDS = [
    "tier", "wallet", "user_name", "flags", "n_resolved", "n_events", "z", "p_value", "q_bh", "q_by", "theta",
    "theta_se", "theta_shrunk", "theta_q20", "hit_rate", "mean_pi", "resolved_pnl", "roi", "top_event_share",
    "pnl_ex_top3", "n_trades", "history_days", "trades_per_active_day", "two_sided_share", "income_share",
    "high_price_buy_share", "median_entry", "top_category",
]  # fmt: skip


def score_row(s: WalletScore) -> dict[str, Any]:
    f, k = s.features, s.skill
    top_category = next(iter(f.category_shares), "")
    return {
        "tier": s.tier,
        "wallet": s.wallet,
        "user_name": s.user_name or "",
        "flags": ",".join(s.flags),
        "n_resolved": k.n_resolved,
        "n_events": k.n_events,
        "z": round(k.z, 3),
        "p_value": round(k.p_value, 6),
        "q_bh": round(s.q_bh, 4),
        "q_by": round(s.q_by, 4),
        "theta": round(k.theta, 4),
        "theta_se": round(k.theta_se, 4) if math.isfinite(k.theta_se) else "",
        "theta_shrunk": round(s.theta_shrunk, 4),
        "theta_q20": round(s.theta_q20, 4) if math.isfinite(s.theta_q20) else "",
        "hit_rate": round(k.hit_rate, 3),
        "mean_pi": round(k.mean_pi, 3),
        "resolved_pnl": round(f.resolved_pnl, 2),
        "roi": round(f.roi, 4),
        "top_event_share": round(f.top_event_share, 3),
        "pnl_ex_top3": round(f.pnl_ex_top3, 2),
        "n_trades": f.n_trades,
        "history_days": round(f.history_days, 1),
        "trades_per_active_day": round(f.trades_per_active_day, 1),
        "two_sided_share": round(f.two_sided_share, 3),
        "income_share": round(f.income_share, 3),
        "high_price_buy_share": round(f.high_price_buy_share, 3),
        "median_entry": round(f.median_entry, 3),
        "top_category": top_category,
    }


def write_scores(scores: list[WalletScore], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".json":
        path.write_text(json.dumps([score_row(s) | {"features": asdict(s.features)} for s in scores], indent=1))
        return
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(score_row(s) for s in scores)


def tier_summary(scores: list[WalletScore]) -> dict[str, Any]:
    from collections import Counter

    return {
        "wallets": len(scores),
        "tiers": dict(Counter(s.tier for s in scores)),
        "flags": dict(Counter(f for s in scores for f in s.flags).most_common()),
    }
