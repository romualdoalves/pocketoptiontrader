"""
universe.py — Dynamic universe selection + market regime + PAXG hedge bias.

Responsibilities:
  1. Filter the core asset list by liquidity (avg daily volume in USD).
  2. Detect market regime from breadth (fraction of assets above their EMA200).
  3. Compute PAXG hedge weight based on regime.
"""

from __future__ import annotations
import logging
from typing import Dict, List

import numpy as np
import pandas as pd

from .indicators import ema

log = logging.getLogger(__name__)


def filter_liquid_assets(
    bars_by_symbol: Dict[str, pd.DataFrame],
    min_avg_daily_volume_usd: float,
    lookback_bars: int,
) -> List[str]:
    """
    Keep only assets whose average dollar volume over the lookback exceeds threshold.
    """
    kept: List[str] = []
    for sym, df in bars_by_symbol.items():
        if df is None or df.empty or len(df) < lookback_bars // 2:
            continue
        window = df.tail(lookback_bars)
        dollar_vol = (window["close"] * window["volume"]).mean()
        if dollar_vol >= min_avg_daily_volume_usd:
            kept.append(sym)
        else:
            log.debug("%s filtered out — avg dollar vol $%,.0f < $%,.0f",
                      sym, dollar_vol, min_avg_daily_volume_usd)
    return kept


def detect_market_regime(
    bars_by_symbol: Dict[str, pd.DataFrame],
    trend_ema_period: int,
    bear_threshold: float,
    bull_threshold: float,
) -> Dict[str, float]:
    """
    Measure market breadth: fraction of core assets trading above their trend EMA.

    Returns:
        {
            "regime": "BULL" | "BEAR" | "NEUTRAL",
            "breadth": float (0.0 to 1.0),
            "n_above": int,
            "n_total": int,
        }
    """
    n_above = 0
    n_total = 0

    for sym, df in bars_by_symbol.items():
        if df is None or df.empty or len(df) < trend_ema_period:
            continue
        n_total += 1
        trend = ema(df["close"], trend_ema_period).iloc[-1]
        if df["close"].iloc[-1] > trend:
            n_above += 1

    if n_total == 0:
        return {"regime": "NEUTRAL", "breadth": 0.5, "n_above": 0, "n_total": 0}

    breadth = n_above / n_total

    if breadth < bear_threshold:
        regime = "BEAR"
    elif breadth > bull_threshold:
        regime = "BULL"
    else:
        regime = "NEUTRAL"

    return {
        "regime":  regime,
        "breadth": round(breadth, 3),
        "n_above": n_above,
        "n_total": n_total,
    }


def compute_hedge_weight(regime: str, cfg: dict) -> float:
    """Look up PAXG weight from regime detection config."""
    if regime == "BULL":
        return float(cfg["hedge_weight_bull"])
    if regime == "BEAR":
        return float(cfg["hedge_weight_bear"])
    return float(cfg["hedge_weight_neutral"])


def select_universe(
    bars_by_symbol: Dict[str, pd.DataFrame],
    core_assets: List[str],
    hedge_asset: str,
    universe_cfg: dict,
    regime_cfg: dict,
) -> dict:
    """
    High-level entry point: returns liquid core list + hedge weight + regime.

    Returns:
        {
            "liquid_core": [...],
            "hedge_asset": "PAXG/USD",
            "hedge_weight": 0.10 .. 0.40,
            "regime": {...},
        }
    """
    core_bars = {s: bars_by_symbol.get(s) for s in core_assets}
    liquid = filter_liquid_assets(
        core_bars,
        universe_cfg["min_avg_daily_volume_usd"],
        universe_cfg["liquidity_lookback_bars"],
    )

    regime = detect_market_regime(
        core_bars,
        regime_cfg["trend_ema_period"],
        regime_cfg["bear_breadth_threshold"],
        regime_cfg["bull_breadth_threshold"],
    )

    hedge_w = compute_hedge_weight(regime["regime"], regime_cfg)

    return {
        "liquid_core":  liquid,
        "hedge_asset":  hedge_asset,
        "hedge_weight": hedge_w,
        "regime":       regime,
    }
