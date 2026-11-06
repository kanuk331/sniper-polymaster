"""Walk-forward plan: independent evaluation periods with fixed dev/holdout roles (config/walkforward.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

DAY = 86400
ROLES = ("dev", "holdout")


@dataclass(frozen=True)
class Period:
    name: str
    split: str
    role: str
    conditions: str = ""
    lookback_days: int = 180
    gap_days: int = 7
    horizon_days: int = 45
    universe_markets: int = 100
    trades_per_market: int = 3000
    min_notional: float = 5000.0
    activity_cap: int = 15000

    @property
    def split_ts(self) -> int:
        return int(datetime.fromisoformat(self.split).replace(tzinfo=UTC).timestamp())

    @property
    def as_of(self) -> int:
        """Scoring cutoff: only information strictly before split - gap is used for selection."""
        return self.split_ts - self.gap_days * DAY

    @property
    def window(self) -> tuple[int, int]:
        """Activity needed per wallet: the scoring lookback plus the evaluation horizon."""
        return self.split_ts - self.lookback_days * DAY, self.eval_end

    @property
    def eval_end(self) -> int:
        return self.split_ts + self.horizon_days * DAY

    @property
    def snapshot(self) -> str:
        return f"wf-{self.name}"

    @property
    def universe_end_dates(self) -> tuple[str, str]:
        start = datetime.fromtimestamp(self.split_ts - self.lookback_days * DAY, UTC)
        end = datetime.fromtimestamp(self.as_of, UTC)
        return f"{start:%Y-%m-%dT%H:%M:%SZ}", f"{end:%Y-%m-%dT%H:%M:%SZ}"


def load_plan(path: Path | str) -> list[Period]:
    data = tomllib.loads(Path(path).read_text())
    defaults = data.get("defaults", {})
    allowed = {f.name for f in fields(Period)}
    periods = []
    for raw in data.get("period", []):
        merged = {k: v for k, v in (defaults | raw).items() if k in allowed}
        period = Period(**merged)
        if period.role not in ROLES:
            raise ValueError(f"{period.name}: role must be one of {ROLES}")
        periods.append(period)
    names = [p.name for p in periods]
    if len(set(names)) != len(names):
        raise ValueError("duplicate period names")
    _check_independent(periods)
    return periods


def _check_independent(periods: list[Period]) -> None:
    spans = sorted((p.split_ts, p.eval_end, p.name) for p in periods)
    for (_, end_a, a), (start_b, _, b) in zip(spans, spans[1:], strict=False):
        if start_b < end_a:
            raise ValueError(f"evaluation windows of {a} and {b} overlap")


def select(periods: list[Period], *, role: str | None = None, names: list[str] | None = None) -> list[Period]:
    out = [p for p in periods if (role is None or role == "all" or p.role == role)]
    if names:
        by_name = {p.name: p for p in out}
        missing = [n for n in names if n not in by_name]
        if missing:
            raise ValueError(f"unknown periods for role {role}: {missing}")
        out = [by_name[n] for n in names]  # caller's order
    return out
