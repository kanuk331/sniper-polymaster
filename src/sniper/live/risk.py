"""Per-variant virtual portfolio risk engine: stake sizing, exposure limits, loss stops and a kill switch.

Limits are fractions of the initial bankroll on the cost basis still deployed. In fixed-stake mode a stake that does
not fit under every limit is rejected (stakes stay uniform, like the backtest); in Kelly mode it is clipped to the
tightest remaining capacity and rejected only if what is left is below `min_stake_usdc`.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

DIMENSIONS = ("market", "event", "leader", "category")
EPS = 1e-9


@dataclass(frozen=True)
class Exposure:
    market: str  # condition id
    event: str  # event key (nr:/ev:/c:)
    leader: str  # wallet
    category: str

    def key(self, dim: str) -> str:
        return getattr(self, dim)


@dataclass(frozen=True)
class RiskConfig:
    bankroll: float = 10_000.0
    stake_mode: str = "fixed"  # "fixed" | "kelly"
    stake_usdc: float = 100.0
    kelly_fraction: float = 0.25
    min_stake_usdc: float = 1.0
    max_market: float = 0.02
    max_event: float = 0.05
    max_leader: float = 0.15
    max_category: float = 0.30
    max_total: float = 0.50
    daily_loss_stop: float = 0.03
    weekly_loss_stop: float = 0.07
    kill_switch_drawdown: float = 0.15  # from peak equity

    def limit(self, dim: str) -> float:
        return getattr(self, f"max_{dim}") * self.bankroll


@dataclass
class RiskDecision:
    stake: float
    reason: str | None = None  # None = approved, else a skip reason such as "risk_event_limit"
    usage: dict[str, float] = field(default_factory=dict)  # share of each limit in use after the trade

    @property
    def approved(self) -> bool:
        return self.reason is None


def kelly_fraction_star(price: float, theta: float, fee_rate: float = 0.0) -> float:
    """Full-Kelly bankroll fraction for buying at `price` when the leader's edge is `theta` log-odds.

    q = sigmoid(logit(price) + theta) is the win probability; the taker fee per share raises the effective price.
    """
    if not 0 < price < 1:
        return 0.0
    q = 1.0 / (1.0 + math.exp(-(math.log(price / (1 - price)) + theta)))
    p_eff = price + fee_rate * price * (1 - price)
    if p_eff >= 1:
        return 0.0
    return max(0.0, (q - p_eff) / (1 - p_eff))


def day_index(ts: float) -> int:
    return int(ts // 86400)


def week_index(ts: float) -> int:
    # epoch day 0 was a Thursday; +3 makes weeks start on Monday (UTC)
    return (day_index(ts) + 3) // 7


class RiskEngine:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.deployed: dict[str, defaultdict[str, float]] = {d: defaultdict(float) for d in DIMENSIONS}
        self.total_deployed = 0.0
        self.realized = 0.0
        self.realized_by_day: defaultdict[int, float] = defaultdict(float)
        self.realized_by_week: defaultdict[int, float] = defaultdict(float)
        self.equity = cfg.bankroll
        self.peak_equity = cfg.bankroll
        self.killed = False
        self.paused = False

    # --- sizing and checks --------------------------------------------------------------------------------------------

    def desired_stake(self, price: float, theta: float, fee_rate: float) -> float:
        if self.cfg.stake_mode == "kelly":
            return max(0.0, self.equity) * self.cfg.kelly_fraction * kelly_fraction_star(price, theta, fee_rate)
        return self.cfg.stake_usdc

    def capacity(self, exp: Exposure) -> dict[str, float]:
        caps = {d: self.cfg.limit(d) - self.deployed[d].get(exp.key(d), 0.0) for d in DIMENSIONS}
        caps["total"] = self.cfg.limit("total") - self.total_deployed
        return caps

    def usage(self, exp: Exposure, extra: float = 0.0) -> dict[str, float]:
        used = {d: self.deployed[d].get(exp.key(d), 0.0) + extra for d in DIMENSIONS}
        used["total"] = self.total_deployed + extra
        return {d: (v / self.cfg.limit(d) if self.cfg.limit(d) > 0 else 0.0) for d, v in used.items()}

    def stop_reason(self, ts: float) -> str | None:
        if self.paused:
            return "paused"
        if self.killed:
            return "risk_kill_switch"
        if self.realized_by_day.get(day_index(ts), 0.0) <= -self.cfg.daily_loss_stop * self.cfg.bankroll + 1e-6:
            return "risk_daily_stop"
        if self.realized_by_week.get(week_index(ts), 0.0) <= -self.cfg.weekly_loss_stop * self.cfg.bankroll + 1e-6:
            return "risk_weekly_stop"
        return None

    def check(self, stake: float, exp: Exposure, ts: float) -> RiskDecision:
        reason = self.stop_reason(ts)
        if reason:
            return RiskDecision(0.0, reason, self.usage(exp))
        caps = self.capacity(exp)
        binding = min(caps, key=lambda d: caps[d])
        approved = stake
        if stake > caps[binding] + EPS:
            if self.cfg.stake_mode != "kelly":
                return RiskDecision(0.0, f"risk_{binding}_limit", self.usage(exp))
            approved = max(0.0, caps[binding])
        if approved < self.cfg.min_stake_usdc:
            return RiskDecision(0.0, f"risk_{binding}_limit", self.usage(exp))
        return RiskDecision(approved, None, self.usage(exp, approved))

    # --- state updates ------------------------------------------------------------------------------------------------

    def open(self, exp: Exposure, cost: float) -> None:
        for d in DIMENSIONS:
            self.deployed[d][exp.key(d)] += cost
        self.total_deployed += cost

    def release(self, exp: Exposure, basis: float) -> None:
        for d in DIMENSIONS:
            book = self.deployed[d]
            left = book.get(exp.key(d), 0.0) - basis
            if left <= EPS:
                book.pop(exp.key(d), None)
            else:
                book[exp.key(d)] = left
        self.total_deployed = max(0.0, self.total_deployed - basis)

    def realize(self, pnl: float, ts: float) -> None:
        self.realized += pnl
        self.realized_by_day[day_index(ts)] += pnl
        self.realized_by_week[week_index(ts)] += pnl

    def update_equity(self, equity: float) -> bool:
        """Track peak equity; returns True when this update trips the kill switch."""
        self.equity = equity
        self.peak_equity = max(self.peak_equity, equity)
        if not self.killed and equity <= self.peak_equity * (1 - self.cfg.kill_switch_drawdown):
            self.killed = True
            return True
        return False

    def resume(self) -> None:
        """Clear a manual pause and the kill switch; the drawdown is measured from current equity again."""
        self.paused = False
        self.killed = False
        self.peak_equity = self.equity

    def state(self) -> dict[str, Any]:
        return {"killed": self.killed, "paused": self.paused, "peak_equity": self.peak_equity}

    def restore(self, state: dict[str, Any]) -> None:
        self.killed = bool(state.get("killed"))
        self.paused = bool(state.get("paused"))
        self.peak_equity = max(self.peak_equity, float(state.get("peak_equity") or 0.0))
