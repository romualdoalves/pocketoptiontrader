"""
macro_filter.py — Binance Futures macro gating.

Funding rate + Open Interest signals used to block longs during unhealthy
conditions (short-covering rallies, over-leveraged long-heavy markets).

Public API only — no credentials required.
"""

from __future__ import annotations
import logging
from typing import Tuple

import requests

log = logging.getLogger(__name__)

BINANCE_FAPI = "https://fapi.binance.com"


def get_funding_rate(symbol: str = "BTCUSDT", threshold: float = 0.0001) -> dict:
    """Latest Binance funding rate. `long_heavy=True` if rate > threshold."""
    try:
        r = requests.get(
            f"{BINANCE_FAPI}/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": 1},
            timeout=5,
        )
        data = r.json()
        rate = float(data[0]["fundingRate"])
        return {"rate": rate, "long_heavy": rate > threshold, "error": None}
    except Exception as e:
        log.warning("Funding rate fetch failed: %s", e)
        return {"rate": 0.0, "long_heavy": False, "error": str(e)}


def get_oi_trend(symbol: str = "BTCUSDT", pct_threshold: float = 0.1) -> dict:
    """
    Open Interest trend over last 30 min (6 × 5min bars).
    Returns trend in {'increasing', 'decreasing', 'neutral'}.
    """
    try:
        r = requests.get(
            f"{BINANCE_FAPI}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": "5m", "limit": 6},
            timeout=5,
        )
        data = r.json()
        if len(data) < 2:
            return {"trend": "neutral", "pct_change": 0.0, "error": "insufficient data"}

        oi_old = float(data[0]["sumOpenInterest"])
        oi_new = float(data[-1]["sumOpenInterest"])
        pct    = (oi_new - oi_old) / oi_old * 100

        if pct > pct_threshold:
            trend = "increasing"
        elif pct < -pct_threshold:
            trend = "decreasing"
        else:
            trend = "neutral"

        return {"trend": trend, "pct_change": round(pct, 4), "error": None}
    except Exception as e:
        log.warning("OI trend fetch failed: %s", e)
        return {"trend": "neutral", "pct_change": 0.0, "error": str(e)}


def macro_allows_long(
    price_rising: bool,
    symbol: str = "BTCUSDT",
    funding_threshold: float = 0.0001,
    oi_threshold: float = 0.1,
) -> Tuple[bool, str, dict]:
    """
    Combined macro gate.
    Blocks longs when:
      - Funding rate > threshold (long-heavy market)
      - Price rising but OI falling (short-covering rally, not real demand)
    Returns (allowed, reason, raw_data).
    """
    funding = get_funding_rate(symbol, funding_threshold)
    oi      = get_oi_trend(symbol, oi_threshold)

    raw = {"funding": funding, "oi": oi}

    if funding["long_heavy"]:
        return False, (
            f"Funding {funding['rate']*100:.4f}% long-heavy > {funding_threshold*100:.4f}%"
        ), raw

    if price_rising and oi["trend"] == "decreasing":
        return False, (
            f"Price rising but OI falling ({oi['pct_change']:.3f}%) — short-covering rally"
        ), raw

    return True, "Macro OK", raw
