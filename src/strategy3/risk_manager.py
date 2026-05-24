"""
risk_manager.py — Adaptive drawdown + cooling period for Strategy 3.

Drawdown tiers (default):
    0 - 2 %  →  100 % exposure
    2 - 4 %  →   80 %
    4 - 6 %  →   40 %
    6 %+     →    0 % (fully hedged / flat)

After hitting 0 % exposure, a cooling period (24 h) starts.
Recovery threshold: must recover 50 % of the drawdown before re-entering.

Equity peak decays after `peak_decay_days` so a months-old ATH doesn't
permanently suppress exposure.
"""

from __future__ import annotations
import datetime
import logging
from typing import Dict, List, Optional

from .state import load_state, save_state, now_iso

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Drawdown → exposure
# ---------------------------------------------------------------------------

def exposure_for_drawdown(dd_pct: float, tiers: List[dict]) -> float:
    """
    Given current drawdown pct and tier config, return allowed exposure (0..1).
    Tiers must be sorted ascending by dd_pct.
    """
    exposure = 1.0
    for tier in sorted(tiers, key=lambda t: t["dd_pct"]):
        if dd_pct >= tier["dd_pct"]:
            exposure = tier["exposure"]
        else:
            break
    return exposure


def compute_drawdown_pct(equity: float, peak: float) -> float:
    """Drawdown as a positive percentage (0 = no DD, 5.0 = 5% below peak)."""
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - equity) / peak * 100.0)


# ---------------------------------------------------------------------------
# Peak decay
# ---------------------------------------------------------------------------

def maybe_decay_peak(
    state: dict,
    peak_decay_days: int,
) -> bool:
    """
    If the equity peak hasn't been updated in `peak_decay_days`, reset it to
    current equity so old ATH doesn't permanently suppress exposure.
    Returns True if peak was decayed.
    """
    ts_str = state.get("equity_peak_timestamp")
    if not ts_str or peak_decay_days <= 0:
        return False

    try:
        ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False

    now = datetime.datetime.now(datetime.timezone.utc)
    days_since = (now - ts).total_seconds() / 86400.0
    if days_since >= peak_decay_days:
        log.info("Equity peak (%.2f) is %.0f days old — decaying.",
                 state["equity_peak"], days_since)
        return True
    return False


# ---------------------------------------------------------------------------
# Cooling period
# ---------------------------------------------------------------------------

def is_cooling(state: dict) -> bool:
    """True if we're in a forced-flat cooling period."""
    until_str = state.get("cooling_until")
    if not until_str:
        return False
    try:
        until = datetime.datetime.fromisoformat(until_str.replace("Z", "+00:00"))
        return datetime.datetime.now(datetime.timezone.utc) < until
    except (ValueError, TypeError):
        return False


def start_cooling(state: dict, hours: int) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc)
    end = now + datetime.timedelta(hours=hours)
    state["cooling_until"] = end.isoformat().replace("+00:00", "Z")
    state["current_exposure"] = 0.0
    log.warning("Cooling period started — flat until %s", state["cooling_until"])
    return state


def check_recovery(
    state: dict,
    equity: float,
    recovery_threshold_pct: float,
) -> bool:
    """
    After cooling ends, check if price has recovered enough (recovery_threshold_pct
    of the drawdown) before re-entering.
    """
    peak = state.get("equity_peak", 0.0)
    if peak <= 0:
        return True
    dd_at_peak = peak - equity
    if dd_at_peak <= 0:
        return True
    recovery = (equity - (peak * (1 - state.get("_dd_at_cooling", 0.06)))) / max(dd_at_peak, 1e-6) * 100
    return recovery >= recovery_threshold_pct


# ---------------------------------------------------------------------------
# Top-level risk gate
# ---------------------------------------------------------------------------

def evaluate_risk(
    equity: float,
    risk_cfg: dict,
    state: Optional[dict] = None,
) -> Dict:
    """
    Main risk evaluation. Updates state in-place and returns:
    {
        "exposure": 0.0 .. 1.0,
        "dd_pct": float,
        "regime": "NORMAL" | "COOLING" | "RECOVERING",
        "peak": float,
        "cooling_until": str | None,
    }
    """
    if state is None:
        state = load_state()

    tiers         = risk_cfg["drawdown_tiers"]
    cooling_hours = risk_cfg.get("cooling_period_hours", 24)
    recovery_thr  = risk_cfg.get("recovery_threshold_pct", 50.0)
    peak_decay    = risk_cfg.get("peak_decay_days", 90)
    max_lev       = risk_cfg.get("max_portfolio_leverage", 1.0)

    # --- Maybe decay stale peak ---
    if maybe_decay_peak(state, peak_decay):
        state["equity_peak"] = equity
        state["equity_peak_timestamp"] = now_iso()

    # --- Update equity peak ---
    if equity > state.get("equity_peak", 0.0):
        state["equity_peak"] = equity
        state["equity_peak_timestamp"] = now_iso()

    peak   = state["equity_peak"]
    dd_pct = compute_drawdown_pct(equity, peak)

    # --- Cooling period ---
    if is_cooling(state):
        result = {
            "exposure": 0.0,
            "dd_pct": dd_pct,
            "regime": "COOLING",
            "peak": peak,
            "cooling_until": state.get("cooling_until"),
        }
        state["current_exposure"] = 0.0
        save_state(state)
        return result

    # --- Post-cooling recovery gate ---
    if state.get("cooling_until") and not is_cooling(state):
        if not check_recovery(state, equity, recovery_thr):
            result = {
                "exposure": 0.0,
                "dd_pct": dd_pct,
                "regime": "RECOVERING",
                "peak": peak,
                "cooling_until": None,
            }
            state["current_exposure"] = 0.0
            save_state(state)
            return result
        else:
            # Recovery achieved — clear cooling state
            state["cooling_until"] = None

    # --- Normal tier-based exposure ---
    exposure = exposure_for_drawdown(dd_pct, tiers)

    # If exposure hit 0 and wasn't already cooling, start cooling
    if exposure <= 0.0 and not state.get("cooling_until"):
        state["_dd_at_cooling"] = dd_pct / 100.0
        state = start_cooling(state, cooling_hours)
        save_state(state)
        return {
            "exposure": 0.0,
            "dd_pct": dd_pct,
            "regime": "COOLING",
            "peak": peak,
            "cooling_until": state.get("cooling_until"),
        }

    exposure = min(exposure, max_lev)
    state["current_exposure"] = exposure
    save_state(state)

    return {
        "exposure": exposure,
        "dd_pct": round(dd_pct, 3),
        "regime": "NORMAL",
        "peak": peak,
        "cooling_until": None,
    }
