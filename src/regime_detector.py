"""
regime_detector.py — Market regime classifier.

Classifies the current market into one of four regimes:
  - "trending_bull":  Strong upward momentum, low volatility
  - "trending_bear":  Strong downward momentum, low volatility
  - "ranging":        Sideways/choppy, no clear trend
  - "volatile":       High VIX or extreme intraday swings

The regime is used by all strategies to scale position sizes via
REGIME_MULTIPLIERS. A "volatile" regime causes most strategies to sit out.

Usage:
    from regime_detector import detect_regime, get_position_multiplier

    regime = detect_regime(prices, vix)
    multiplier = get_position_multiplier("trailing_stop", regime)
    adjusted_size = base_size_pct * multiplier
"""

import numpy as np
import os
import json
import datetime

# Per-strategy multipliers for each regime.
# 1.0 = full size, 0.5 = half size, 0.0 = skip entirely.
REGIME_MULTIPLIERS = {
    "trailing_stop": {
        "trending_bull": 1.0,
        "ranging":       0.5,
        "volatile":      0.0,
        "trending_bear": 0.3,
    },
    "ladder_buys": {
        "trending_bull": 0.5,
        "ranging":       1.0,
        "volatile":      0.5,
        "trending_bear": 1.0,
    },
    "wheel": {
        "trending_bull": 1.0,
        "ranging":       1.0,
        "volatile":      0.3,
        "trending_bear": 0.5,
    },
    "copy_trading": {
        "trending_bull": 1.0,
        "ranging":       0.7,
        "volatile":      0.3,
        "trending_bear": 0.3,
    },
}

# Thresholds (tuneable)
VIX_VOLATILE_THRESHOLD  = 30.0   # VIX above this → "volatile"
TREND_STRONG_THRESHOLD  = 0.001  # 20-day avg daily return above/below this → trending
VOL_QUIET_THRESHOLD     = 0.015  # 20-day std of daily returns below this → not volatile


def detect_regime(prices: list[float], vix: float) -> str:
    """
    Classify market regime from a price series and a VIX reading.

    Args:
        prices: List of daily closing prices, most recent last.
                Needs at least 21 values for reliable results.
        vix:    Current CBOE VIX level (e.g. 18.5, 32.0).

    Returns:
        One of: "trending_bull", "trending_bear", "ranging", "volatile"
    """
    if vix > VIX_VOLATILE_THRESHOLD:
        return "volatile"

    if len(prices) < 21:
        # Not enough history — treat as ranging to be conservative
        return "ranging"

    prices_arr = np.array(prices[-21:], dtype=float)
    returns = np.diff(prices_arr) / prices_arr[:-1]

    trend = float(np.mean(returns))
    vol = float(np.std(returns))

    if vol >= VOL_QUIET_THRESHOLD:
        return "volatile"
    elif trend > TREND_STRONG_THRESHOLD:
        return "trending_bull"
    elif trend < -TREND_STRONG_THRESHOLD:
        return "trending_bear"
    else:
        return "ranging"


def get_position_multiplier(strategy: str, regime: str) -> float:
    """
    Return the position-size multiplier for a given strategy in a given regime.

    Args:
        strategy: One of "trailing_stop", "ladder_buys", "wheel", "copy_trading"
        regime:   Output of detect_regime()

    Returns:
        Float multiplier in [0.0, 1.0].
        Multiply base position_size_pct by this value before placing orders.
    """
    strategy_map = REGIME_MULTIPLIERS.get(strategy)
    if not strategy_map:
        raise ValueError(f"Unknown strategy '{strategy}'. "
                         f"Valid: {list(REGIME_MULTIPLIERS.keys())}")
    return strategy_map.get(regime, 1.0)


def save_regime_snapshot(regime: str, vix: float, prices: list[float]) -> None:
    """
    Persist the current regime classification to logs/regime_snapshot.json
    so prompt scripts can read it without re-computing.
    """
    log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
    os.makedirs(log_dir, exist_ok=True)

    snapshot = {
        "timestamp":  datetime.datetime.utcnow().isoformat() + "Z",
        "regime":     regime,
        "vix":        vix,
        "price_last": prices[-1] if prices else None,
        "multipliers": {s: REGIME_MULTIPLIERS[s][regime] for s in REGIME_MULTIPLIERS},
    }

    path = os.path.join(log_dir, "regime_snapshot.json")
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2)


def load_regime_snapshot() -> dict | None:
    """Load the most recently saved regime snapshot, or None if not found."""
    path = os.path.join(os.path.dirname(__file__), "..", "logs", "regime_snapshot.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


if __name__ == "__main__":
    # Quick sanity check with synthetic data
    import random

    random.seed(42)
    bull_prices = [100 * (1 + random.gauss(0.002, 0.008)) ** i for i in range(30)]
    bear_prices = [100 * (1 + random.gauss(-0.002, 0.008)) ** i for i in range(30)]
    range_prices = [100 + random.gauss(0, 0.5) for _ in range(30)]

    print("Bull prices regime:", detect_regime(bull_prices, vix=15))
    print("Bear prices regime:", detect_regime(bear_prices, vix=15))
    print("Ranging prices regime:", detect_regime(range_prices, vix=15))
    print("High VIX regime:", detect_regime(bull_prices, vix=35))

    print("\nMultiplier for trailing_stop in trending_bull:",
          get_position_multiplier("trailing_stop", "trending_bull"))
    print("Multiplier for trailing_stop in volatile:",
          get_position_multiplier("trailing_stop", "volatile"))
