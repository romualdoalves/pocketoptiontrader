"""
allocator.py — Volatility-aware portfolio allocation for Strategy 3.

Combines inverse-volatility weighting with Sharpe-ratio weighting in a
configurable ratio (default 50:50). Applies per-asset min/max weight caps
and re-normalizes so weights sum to 1.0 (before hedge carve-out).
"""

from __future__ import annotations
import logging
from typing import Dict, List

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Component weight schemes
# ---------------------------------------------------------------------------

def inverse_volatility_weights(
    bars_by_symbol: Dict[str, pd.DataFrame],
    symbols: List[str],
    lookback: int,
) -> Dict[str, float]:
    """Weight each asset inversely proportional to its recent realized vol."""
    inv_vols: Dict[str, float] = {}
    for sym in symbols:
        df = bars_by_symbol.get(sym)
        if df is None or len(df) < lookback // 2:
            continue
        rets = np.log(df["close"].tail(lookback) / df["close"].tail(lookback).shift(1)).dropna()
        vol = float(rets.std())
        if vol > 1e-12:
            inv_vols[sym] = 1.0 / vol
        else:
            inv_vols[sym] = 0.0

    total = sum(inv_vols.values()) or 1.0
    return {s: v / total for s, v in inv_vols.items()}


def sharpe_weights(
    bars_by_symbol: Dict[str, pd.DataFrame],
    symbols: List[str],
    lookback: int,
) -> Dict[str, float]:
    """Weight by annualized Sharpe ratio (floored at 0 — no negative allocation)."""
    sharpes: Dict[str, float] = {}
    for sym in symbols:
        df = bars_by_symbol.get(sym)
        if df is None or len(df) < lookback // 2:
            continue
        rets = np.log(df["close"].tail(lookback) / df["close"].tail(lookback).shift(1)).dropna()
        mu  = float(rets.mean())
        std = float(rets.std())
        if std > 1e-12:
            sr = mu / std  # per-bar Sharpe (annualization cancels in ratio)
        else:
            sr = 0.0
        sharpes[sym] = max(sr, 0.0)  # floor negatives

    total = sum(sharpes.values()) or 1.0
    return {s: v / total for s, v in sharpes.items()}


# ---------------------------------------------------------------------------
# Hybrid combiner
# ---------------------------------------------------------------------------

def hybrid_weights(
    bars_by_symbol: Dict[str, pd.DataFrame],
    symbols: List[str],
    vol_lookback: int,
    sharpe_lookback: int,
    invvol_weight: float = 0.5,
    sharpe_weight: float = 0.5,
) -> Dict[str, float]:
    """
    Blend inverse-vol and Sharpe weights.
    Falls back to equal weight if both components are all-zero.
    """
    iv = inverse_volatility_weights(bars_by_symbol, symbols, vol_lookback)
    sw = sharpe_weights(bars_by_symbol, symbols, sharpe_lookback)

    all_syms = sorted(set(iv.keys()) | set(sw.keys()))
    if not all_syms:
        return {}

    blended: Dict[str, float] = {}
    for s in all_syms:
        blended[s] = invvol_weight * iv.get(s, 0.0) + sharpe_weight * sw.get(s, 0.0)

    total = sum(blended.values())
    if total < 1e-12:
        # fallback: equal weight
        eq = 1.0 / len(all_syms)
        return {s: eq for s in all_syms}

    return {s: v / total for s, v in blended.items()}


# ---------------------------------------------------------------------------
# Cap + normalize
# ---------------------------------------------------------------------------

def clamp_weights(
    raw: Dict[str, float],
    min_w: float,
    max_w: float,
) -> Dict[str, float]:
    """
    Apply per-asset floor/ceiling and re-normalize to sum = 1.0.
    Assets below min_w are dropped entirely to avoid dust positions.
    """
    clamped = {s: min(w, max_w) for s, w in raw.items() if w >= min_w}
    if not clamped:
        return {}
    total = sum(clamped.values()) or 1.0
    return {s: round(v / total, 6) for s, v in clamped.items()}


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------

def compute_target_weights(
    bars_by_symbol: Dict[str, pd.DataFrame],
    liquid_core: List[str],
    hedge_asset: str,
    hedge_weight: float,
    alpha_signals: Dict[str, int],
    allocator_cfg: dict,
) -> Dict[str, float]:
    """
    Compute final target portfolio weights.

    1. Compute hybrid weights across liquid_core.
    2. Apply alpha overlay: zero out assets with negative LSTM signal,
       boost assets with positive signal.
    3. Clamp per min/max.
    4. Carve out hedge_weight for the hedge asset.

    Returns dict of symbol → weight (sums to 1.0).
    """
    if not liquid_core:
        return {hedge_asset: 1.0} if hedge_asset else {}

    method = allocator_cfg.get("method", "hybrid")

    if method == "invvol":
        raw = inverse_volatility_weights(
            bars_by_symbol, liquid_core, allocator_cfg["vol_lookback_bars"])
    elif method == "sharpe":
        raw = sharpe_weights(
            bars_by_symbol, liquid_core, allocator_cfg["sharpe_lookback_bars"])
    else:  # hybrid (default)
        raw = hybrid_weights(
            bars_by_symbol, liquid_core,
            vol_lookback=allocator_cfg["vol_lookback_bars"],
            sharpe_lookback=allocator_cfg["sharpe_lookback_bars"],
            invvol_weight=allocator_cfg.get("invvol_weight", 0.5),
            sharpe_weight=allocator_cfg.get("sharpe_weight", 0.5),
        )

    # --- Alpha overlay ---
    for sym in list(raw.keys()):
        sig = alpha_signals.get(sym, 0)
        if sig < 0:
            raw[sym] *= 0.25  # penalize bearish signal
        elif sig > 0:
            raw[sym] *= 1.5   # boost bullish signal

    # Re-normalize after overlay
    total = sum(raw.values()) or 1.0
    raw = {s: v / total for s, v in raw.items()}

    # --- Clamp ---
    clamped = clamp_weights(
        raw,
        allocator_cfg.get("min_weight_per_asset", 0.02),
        allocator_cfg.get("max_weight_per_asset", 0.40),
    )

    if not clamped:
        return {hedge_asset: 1.0} if hedge_asset else {}

    # --- Carve out hedge ---
    core_fraction = 1.0 - hedge_weight
    final: Dict[str, float] = {}
    for s, w in clamped.items():
        final[s] = round(w * core_fraction, 6)

    if hedge_asset:
        final[hedge_asset] = round(hedge_weight, 6)

    return final


def needs_rebalance(
    current_weights: Dict[str, float],
    target_weights: Dict[str, float],
    threshold_pct: float,
) -> bool:
    """True if any asset's weight drifts more than threshold_pct from target."""
    all_syms = set(current_weights.keys()) | set(target_weights.keys())
    for s in all_syms:
        cur = current_weights.get(s, 0.0)
        tgt = target_weights.get(s, 0.0)
        if abs(cur - tgt) * 100 > threshold_pct:
            return True
    return False
