"""Watchlist from a scores CSV (`sniper score` output): who is paper-tracked, who triggers alerts, control group."""

from __future__ import annotations

import csv
import random
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROLES = ("alert", "track", "control", "debug")


@dataclass(frozen=True)
class WatchEntry:
    wallet: str
    tier: str
    role: str = "track"  # alert: paper + Telegram; track/control: paper only; debug: smoke-test signal
    user_name: str = ""
    theta_shrunk: float = 0.0
    theta_q20: float | None = None
    top_category: str = ""
    style: str = ""

    @property
    def alerts(self) -> bool:
        return self.role == "alert"


def _float(text: Any, default: float | None = None) -> float | None:
    try:
        return float(text) if text not in (None, "") else default
    except (TypeError, ValueError):
        return default


def entry_from_row(row: dict[str, Any], role: str) -> WatchEntry:
    return WatchEntry(
        wallet=str(row.get("wallet") or "").strip().lower(),
        tier=str(row.get("tier") or "").strip(),
        role=role,
        user_name=str(row.get("user_name") or "").strip(),
        theta_shrunk=_float(row.get("theta_shrunk"), 0.0) or 0.0,
        theta_q20=_float(row.get("theta_q20")),
        top_category=str(row.get("top_category") or "").strip(),
        style=str(row.get("style") or "").strip(),
    )


@dataclass
class Watchlist:
    entries: dict[str, WatchEntry] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.wallets = frozenset(self.entries)  # O(1) membership on the global feed

    def __contains__(self, wallet: object) -> bool:
        return wallet in self.wallets

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, wallet: str) -> WatchEntry | None:
        return self.entries.get(wallet)

    def by_role(self, *roles: str) -> list[WatchEntry]:
        return [e for e in self.entries.values() if e.role in roles]

    def counts(self) -> dict[str, int]:
        return dict(Counter(f"{e.role}:{e.tier}" for e in self.entries.values()))


def load_watchlist(
    path: Path | str,
    *,
    track_tiers: Iterable[str] = ("candidate", "watch"),
    alert_tiers: Iterable[str] = ("candidate",),
    control_sample: int = 0,
    control_tier: str = "noise",
    seed: int = 0,
) -> Watchlist:
    """Alert tiers are always tracked. The control group is a seeded sample of `control_tier`, paper-tracked only."""
    alert = set(alert_tiers)
    tracked = set(track_tiers) | alert
    entries: dict[str, WatchEntry] = {}
    pool: list[dict[str, Any]] = []
    with Path(path).open(newline="") as fh:
        for row in csv.DictReader(fh):
            wallet = str(row.get("wallet") or "").strip().lower()
            tier = str(row.get("tier") or "").strip()
            if not wallet or wallet in entries:
                continue
            if tier in tracked:
                entries[wallet] = entry_from_row(row, "alert" if tier in alert else "track")
            elif tier == control_tier:
                pool.append(row)
    if control_sample > 0 and pool:
        pool.sort(key=lambda r: str(r.get("wallet")).lower())
        for row in random.Random(seed).sample(pool, min(control_sample, len(pool))):
            entry = entry_from_row(row, "control")
            entries.setdefault(entry.wallet, entry)
    return Watchlist(entries)
