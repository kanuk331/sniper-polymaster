"""Per-wallet analytics: market-level bets, cash-flow PnL, archetype features and skill-vs-luck statistics.

Notation: a wallet's bet on a binary market (condition) is summarised by the shares bought of each outcome. The
dominant outcome d is the one with more shares; pi_d is the wallet's average entry price for d, i.e. the market's
implied probability when the wallet entered. Under "no skill" the dominant outcome wins with probability pi_d.
Outcome 0 is used as the reference indicator y0 (for neg-risk markets outcome 0 is "Yes").
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

import numpy as np

Row = Mapping[str, Any]

INCOME_TYPES = frozenset({"REWARD", "MAKER_REBATE", "TAKER_REBATE", "REFERRAL_REWARD", "YIELD", "SPONSORED_REWARD"})
SHORT_CRYPTO_MARKERS = ("updown", "up-or-down")
PI_CLIP = 0.005


@dataclass(frozen=True)
class TokenMeta:
    token_id: str
    condition_id: str
    outcome_index: int
    resolved: bool
    final_price: float | None
    event_key: str
    neg_risk: bool


@dataclass(frozen=True)
class MarketMeta:
    condition_id: str
    category: str
    fee_rate: float
    closed_time: int | None


@dataclass
class Meta:
    tokens: dict[str, TokenMeta]
    markets: dict[str, MarketMeta]
    by_condition: dict[str, dict[int, TokenMeta]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for t in self.tokens.values():
            self.by_condition.setdefault(t.condition_id, {})[t.outcome_index] = t

    def resolution(self, condition_id: str, as_of: int | None) -> float | None:
        """Payout of outcome 0 if the market resolved before `as_of` (None = no cutoff), else None."""
        outcomes = self.by_condition.get(condition_id) or {}
        known = [t for t in outcomes.values() if t.resolved and t.final_price is not None]
        if not known:
            return None
        if as_of is not None:
            market = self.markets.get(condition_id)
            # Unknown close time with a cutoff: treat as unresolved rather than risk look-ahead.
            if market is None or market.closed_time is None or market.closed_time >= as_of:
                return None
        t = known[0]
        return float(t.final_price) if t.outcome_index == 0 else 1.0 - float(t.final_price)


def event_key_for(row: Row) -> str:
    if row["neg_risk"] and row["neg_risk_market_id"]:
        return f"nr:{row['neg_risk_market_id']}"
    if row["event_id"]:
        return f"ev:{row['event_id']}"
    return f"c:{row['condition_id']}"


def load_meta(db: Any, *, wallets: Iterable[str] | None = None, start: int = 0, end: int = 2**62) -> Meta:
    """Token and market metadata; restricted to the conditions `wallets` touched in [start, end) if given."""
    cids = db.condition_ids_for(wallets, start, end) if wallets is not None else None
    tokens = {
        tid: TokenMeta(
            token_id=tid,
            condition_id=r["condition_id"],
            outcome_index=int(r["outcome_index"] or 0),
            resolved=bool(r["resolved"]),
            final_price=r["final_price"],
            event_key=event_key_for(r),
            neg_risk=bool(r["neg_risk"]),
        )
        for tid, r in db.tokens(cids).items()
    }
    markets = {
        cid: MarketMeta(cid, r["category"] or "unknown", float(r["fee_rate"] or 0.0), r["closed_time"])
        for cid, r in db.markets(cids).items()
    }
    return Meta(tokens, markets)


# --- bets ----------------------------------------------------------------------------------------------------------


@dataclass
class Bet:
    condition_id: str
    event_key: str
    category: str
    neg_risk: bool
    first_ts: int
    shares: list[float]
    cost: list[float]
    y0: float | None = None  # payout of outcome 0, None while unresolved (as of the cutoff)

    @property
    def dominant(self) -> int:
        return 0 if self.shares[0] >= self.shares[1] else 1

    @property
    def sign(self) -> int:
        return 1 if self.dominant == 0 else -1

    @property
    def net_shares0(self) -> float:
        return self.shares[0] - self.shares[1]

    @property
    def pi_dominant(self) -> float:
        d = self.dominant
        return self.cost[d] / self.shares[d]

    @property
    def pi0(self) -> float:
        """Implied probability of outcome 0, taken from the entry price of the dominant side."""
        return self.pi_dominant if self.dominant == 0 else 1.0 - self.pi_dominant

    @property
    def y_dominant(self) -> float | None:
        if self.y0 is None:
            return None
        return self.y0 if self.dominant == 0 else 1.0 - self.y0

    @property
    def two_sided(self) -> bool:
        lo, hi = sorted(self.shares)
        return lo > 0 and lo / hi >= 0.2

    @property
    def notional(self) -> float:
        return sum(self.cost)


def build_bets(rows: Iterable[Row], meta: Meta, *, as_of: int | None = None) -> list[Bet]:
    """Aggregate BUY fills (strictly before `as_of`) into one bet per market."""
    bets: dict[str, Bet] = {}
    for r in rows:
        if r["type"] != "TRADE" or r["side"] != "BUY" or (as_of is not None and r["ts"] >= as_of):
            continue
        token = meta.tokens.get(r["token_id"])
        if token is None or token.outcome_index not in (0, 1) or r["size"] <= 0:
            continue
        bet = bets.get(token.condition_id)
        if bet is None:
            market = meta.markets.get(token.condition_id)
            bet = bets[token.condition_id] = Bet(
                condition_id=token.condition_id,
                event_key=token.event_key,
                category=market.category if market else "unknown",
                neg_risk=token.neg_risk,
                first_ts=r["ts"],
                shares=[0.0, 0.0],
                cost=[0.0, 0.0],
            )
        bet.shares[token.outcome_index] += r["size"]
        bet.cost[token.outcome_index] += r["size"] * r["price"]
        bet.first_ts = min(bet.first_ts, r["ts"])
    for bet in bets.values():
        bet.y0 = meta.resolution(bet.condition_id, as_of)
    return list(bets.values())


# --- skill statistics --------------------------------------------------------------------------------------------


def _clip(p: float) -> float:
    return min(1 - PI_CLIP, max(PI_CLIP, p))


def event_variance(items: Sequence[tuple[float, float]], *, mutually_exclusive: bool) -> float:
    """Null variance of sum(a_c * y_c) for the markets of one event; items = (a_c, pi_c) with pi_c = P(y_c = 1).

    Neg-risk events: at most one market resolves Yes, so Var = sum(a^2 pi) - (sum(a pi))^2 (pi rescaled if the
    wallet's entry prices sum above 1). Other events (e.g. moneyline + spread of one game) have unknown
    correlation; we use the perfect-correlation upper bound (sum |a| sigma)^2, which counts the event as one bet.
    """
    if len(items) == 1:
        a, p = items[0]
        return a * a * p * (1 - p)
    if mutually_exclusive:
        total = sum(p for _, p in items)
        scale = 1.0 / total if total > 1 else 1.0
        ps = [(a, p * scale) for a, p in items]
        return max(0.0, sum(a * a * p for a, p in ps) - sum(a * p for a, p in ps) ** 2)
    return sum(abs(a) * math.sqrt(p * (1 - p)) for a, p in items) ** 2


def clustered_variance(bets: Sequence[Bet], weights: Sequence[float]) -> float:
    groups: dict[str, list[tuple[float, float]]] = defaultdict(list)
    exclusive: dict[str, bool] = {}
    for bet, w in zip(bets, weights, strict=True):
        groups[bet.event_key].append((w, _clip(bet.pi0)))
        exclusive[bet.event_key] = bet.neg_risk
    return sum(event_variance(items, mutually_exclusive=exclusive[k]) for k, items in groups.items())


def logit_offset_mle(pis: np.ndarray, ys: np.ndarray, max_iter: int = 50) -> tuple[float, float]:
    """MLE of theta in logit P(y=1) = logit(pi) + theta and its (unclustered) standard error 1/sqrt(Fisher info)."""
    if len(pis) == 0:
        return 0.0, math.inf
    offset = np.log(pis / (1 - pis))
    theta = 0.0
    info = 0.0
    for _ in range(max_iter):
        mu = 1.0 / (1.0 + np.exp(-(offset + theta)))
        score = float(np.sum(ys - mu))
        info = float(np.sum(mu * (1 - mu)))
        if info <= 0:
            break
        step = score / info
        theta = float(np.clip(theta + step, -5.0, 5.0))
        if abs(step) < 1e-9:
            break
    return theta, (1.0 / math.sqrt(info) if info > 0 else math.inf)


def normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


@dataclass
class SkillStats:
    n_bets: int
    n_resolved: int
    n_events: int
    excess: float  # sum over resolved bets of (y_d - pi_d): wins above what entry prices implied
    z: float  # event-clustered, each market = one unit bet
    p_value: float  # one-sided H0: no edge over entry prices
    z_weighted: float  # same with net shares as weights
    theta: float  # log-odds edge over the entry price
    theta_se: float  # inflated by the clustering design effect
    hit_rate: float
    mean_pi: float
    n_eff: float  # effective number of bets given size concentration


def skill_stats(bets: Sequence[Bet]) -> SkillStats:
    resolved = [b for b in bets if b.y0 is not None]
    n_events = len({b.event_key for b in resolved})
    if not resolved:
        return SkillStats(len(bets), 0, 0, 0.0, 0.0, 1.0, 0.0, 0.0, math.inf, 0.0, 0.0, 0.0)
    signs = [float(b.sign) for b in resolved]
    excess = sum(s * (b.y0 - _clip(b.pi0)) for s, b in zip(signs, resolved, strict=True))
    var_c = clustered_variance(resolved, signs)
    var_i = sum(_clip(b.pi0) * (1 - _clip(b.pi0)) for b in resolved)
    z = excess / math.sqrt(var_c) if var_c > 0 else 0.0

    w = [b.net_shares0 for b in resolved]
    excess_w = sum(wi * (b.y0 - _clip(b.pi0)) for wi, b in zip(w, resolved, strict=True))
    var_w = clustered_variance(resolved, w)
    z_w = excess_w / math.sqrt(var_w) if var_w > 0 else 0.0

    pis = np.array([_clip(b.pi_dominant) for b in resolved])
    ys = np.array([b.y_dominant for b in resolved], dtype=float)
    theta, se = logit_offset_mle(pis, ys)
    design_effect = max(1.0, var_c / var_i) if var_i > 0 else 1.0
    abs_w = np.abs(np.array(w))
    n_eff = float(abs_w.sum() ** 2 / np.sum(abs_w**2)) if abs_w.sum() > 0 else 0.0
    return SkillStats(
        n_bets=len(bets),
        n_resolved=len(resolved),
        n_events=n_events,
        excess=excess,
        z=z,
        p_value=normal_sf(z),
        z_weighted=z_w,
        theta=theta,
        theta_se=se * math.sqrt(design_effect),
        hit_rate=float(np.mean(ys > 0.5)),
        mean_pi=float(np.mean(pis)),
        n_eff=n_eff,
    )


# --- multiple testing and shrinkage ------------------------------------------------------------------------------


def bh_qvalues(p_values: Sequence[float], *, dependent: bool = False) -> list[float]:
    """Benjamini-Hochberg q-values; dependent=True gives Benjamini-Yekutieli (valid under any dependence)."""
    m = len(p_values)
    if m == 0:
        return []
    c_m = sum(1.0 / k for k in range(1, m + 1)) if dependent else 1.0
    order = sorted(range(m), key=lambda i: p_values[i])
    q = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, p_values[i] * m * c_m / rank)
        q[i] = min(1.0, running)
    return q


@dataclass
class Shrunk:
    mean: float
    sd: float
    prior_mean: float
    prior_var: float


def dersimonian_laird_tau2(thetas: np.ndarray, ses: np.ndarray) -> float:
    """Between-wallet variance of true edges (DerSimonian-Laird). Unlike the plain moment estimator
    var(theta) - mean(se^2), it is not swamped by a few wallets with huge standard errors."""
    w = 1.0 / ses**2
    mean_w = float(np.sum(w * thetas) / np.sum(w))
    q = float(np.sum(w * (thetas - mean_w) ** 2))
    denom = float(np.sum(w) - np.sum(w**2) / np.sum(w))
    return max(0.0, (q - (len(thetas) - 1)) / denom) if denom > 0 else 0.0


def empirical_bayes(thetas: Sequence[float], ses: Sequence[float]) -> list[Shrunk]:
    """Normal-normal shrinkage (Efron-Morris): theta_i ~ N(mu, tau^2), theta_hat_i ~ N(theta_i, se_i^2)."""
    finite = [(t, s) for t, s in zip(thetas, ses, strict=True) if math.isfinite(s) and s > 0]
    if len(finite) < 3:
        return [Shrunk(t, s, t, math.inf) for t, s in zip(thetas, ses, strict=True)]
    ts = np.array([t for t, _ in finite])
    ss = np.array([s for _, s in finite])
    tau2 = dersimonian_laird_tau2(ts, ss)
    w = 1.0 / (tau2 + ss**2)
    mu = float(np.sum(w * ts) / np.sum(w))
    # Uncertainty of the estimated prior mean; without it tau2 = 0 would give a spuriously exact posterior.
    var_mu = float(1.0 / np.sum(w))
    out = []
    for t, s in zip(thetas, ses, strict=True):
        if not math.isfinite(s) or s <= 0:
            out.append(Shrunk(mu, math.sqrt(tau2 + var_mu), mu, tau2))
            continue
        k = tau2 / (tau2 + s * s)
        out.append(Shrunk(mu + k * (t - mu), math.sqrt(k * s * s + (1 - k) ** 2 * var_mu), mu, tau2))
    return out


# --- PnL and archetype features ----------------------------------------------------------------------------------


def resolved_condition_pnl(rows: Iterable[Row], meta: Meta, *, as_of: int | None = None) -> dict[str, float]:
    """Cash-flow PnL of markets resolved before `as_of`: trades, splits, merges, redeems, plus unredeemed shares.

    Neg-risk CONVERSION flows are not attributable to a single market and are ignored (such wallets are flagged
    as arbitrage separately).
    """
    cash: dict[str, float] = defaultdict(float)
    shares: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if as_of is not None and r["ts"] >= as_of:
            continue
        kind = r["type"]
        token = meta.tokens.get(r["token_id"]) if r["token_id"] else None
        cid = token.condition_id if token else r["condition_id"]
        if kind == "TRADE" and token is not None:
            sign = -1.0 if r["side"] == "BUY" else 1.0
            cash[cid] += sign * r["size"] * r["price"]
            shares[cid][token.outcome_index] -= sign * r["size"]
        elif kind in ("MERGE", "SPLIT"):
            sign = 1.0 if kind == "MERGE" else -1.0
            cash[cid] += sign * r["usdc_size"]
            for i in (0, 1):
                shares[cid][i] -= sign * r["size"]
        elif kind == "REDEEM":
            cash[cid] += r["usdc_size"]
            if token is not None:
                shares[cid][token.outcome_index] -= r["size"]
            else:
                shares[cid].clear()
    out: dict[str, float] = {}
    for cid, flow in cash.items():
        y0 = meta.resolution(cid, as_of)
        if y0 is None:
            continue
        held = shares.get(cid, {})
        value = max(0.0, held.get(0, 0.0)) * y0 + max(0.0, held.get(1, 0.0)) * (1.0 - y0)
        out[cid] = flow + value
    return out


@dataclass
class Features:
    n_trades: int
    active_days: int
    history_days: float
    trades_per_active_day: float
    short_crypto_share: float
    two_sided_share: float
    merge_conversion_share: float
    income_usdc: float
    income_share: float
    high_price_buy_share: float
    median_entry: float
    sell_share: float
    resolved_pnl: float
    resolved_cost: float
    roi: float
    top_event_share: float
    pnl_ex_top3: float
    n_events_resolved: int
    category_shares: dict[str, float]


def features(rows: Sequence[Row], meta: Meta, bets: Sequence[Bet], *, as_of: int | None = None) -> Features:
    rows = [r for r in rows if as_of is None or r["ts"] < as_of]
    trades = [r for r in rows if r["type"] == "TRADE"]
    buys = [r for r in trades if r["side"] == "BUY"]
    days = {r["ts"] // 86400 for r in trades}
    stamps = [r["ts"] for r in trades]
    history_days = (max(stamps) - min(stamps)) / 86400 if stamps else 0.0
    buy_notional = sum(r["size"] * r["price"] for r in buys)
    short_crypto = sum(1 for r in trades if any(m in (r["slug"] or "") for m in SHORT_CRYPTO_MARKERS))
    traded_conditions = {meta.tokens[r["token_id"]].condition_id for r in trades if r["token_id"] in meta.tokens}
    structural = {r["condition_id"] for r in rows if r["type"] in ("MERGE", "SPLIT", "CONVERSION")}
    structural_share = len(structural) / max(1, len(traded_conditions | structural))
    income = sum(r["usdc_size"] for r in rows if r["type"] in INCOME_TYPES)

    pnl_by_condition = resolved_condition_pnl(rows, meta, as_of=as_of)
    event_of = {b.condition_id: b.event_key for b in bets}
    pnl_by_event: dict[str, float] = defaultdict(float)
    for cid, pnl in pnl_by_condition.items():
        pnl_by_event[event_of.get(cid, f"c:{cid}")] += pnl
    total = sum(pnl_by_event.values())
    ranked = sorted(pnl_by_event.values(), reverse=True)
    resolved_bets = [b for b in bets if b.y0 is not None]
    resolved_cost = sum(b.notional for b in resolved_bets)
    categories: dict[str, float] = defaultdict(float)
    for b in bets:
        categories[b.category] += b.notional
    total_notional = sum(categories.values()) or 1.0

    return Features(
        n_trades=len(trades),
        active_days=len(days),
        history_days=history_days,
        trades_per_active_day=len(trades) / max(1, len(days)),
        short_crypto_share=short_crypto / max(1, len(trades)),
        two_sided_share=sum(b.two_sided for b in bets) / max(1, len(bets)),
        merge_conversion_share=structural_share,
        income_usdc=income,
        income_share=income / (income + max(total, 0.0)) if income > 0 else 0.0,
        high_price_buy_share=sum(r["size"] * r["price"] for r in buys if r["price"] >= 0.95) / buy_notional
        if buy_notional
        else 0.0,
        median_entry=median(b.pi_dominant for b in bets) if bets else 0.0,
        sell_share=1 - len(buys) / max(1, len(trades)),
        resolved_pnl=total,
        resolved_cost=resolved_cost,
        roi=total / resolved_cost if resolved_cost else 0.0,
        top_event_share=ranked[0] / total if ranked and total > 0 else 0.0,
        pnl_ex_top3=total - sum(x for x in ranked[:3] if x > 0),
        n_events_resolved=len({b.event_key for b in resolved_bets}),
        category_shares={k: v / total_notional for k, v in sorted(categories.items(), key=lambda kv: -kv[1])},
    )
