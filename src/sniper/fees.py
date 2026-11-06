"""Historical taker-fee regime.

Gamma's `feeSchedule` describes a market's fees today; applying it to trades made before fees existed would
overstate copy costs in older periods. Dates from docs.polymarket.com/changelog/predictions (checked 2026-11-06):

- 2026-01-05  taker fees on 15-minute crypto markets (5-minute markets launched with fees on 2026-02-12)
- 2026-02-18  NCAAB and Serie A markets (league is not identifiable from our data; not modelled)
- 2026-03-06  all newly created crypto markets
- 2026-03-30  Fee Structure V2: crypto, sports, finance, politics, economics, culture, weather, tech, mentions,
              other; geopolitics stays free
- 2026-07-10  sports rate 0.03 -> 0.05
"""

from __future__ import annotations

from datetime import UTC, datetime


def _ts(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=UTC).timestamp())


CRYPTO_FEES_START = _ts(2026, 1, 5)
FEE_V2_START = _ts(2026, 3, 30)
SPORTS_HIKE = _ts(2026, 7, 10)
SPORTS_RATE_BEFORE_HIKE = 0.03


def taker_fee_rate(category: str, market_rate: float, ts: int) -> float:
    """Fee rate in force for a taker trade at `ts`, given the market's current schedule rate."""
    if market_rate <= 0:
        return 0.0
    if ts >= FEE_V2_START:
        if category == "sports" and ts < SPORTS_HIKE:
            return min(market_rate, SPORTS_RATE_BEFORE_HIKE)
        return market_rate
    if category == "crypto" and ts >= CRYPTO_FEES_START:
        return market_rate
    return 0.0


def taker_fee(shares: float, price: float, rate: float) -> float:
    # docs.polymarket.com/trading/fees: fee = C * feeRate * p * (1 - p)
    return shares * rate * price * (1 - price)
