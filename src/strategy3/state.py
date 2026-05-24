"""
state.py — Persistent JSON state for Strategy 3.

Tracks:
  • equity_peak            (highest portfolio value seen)
  • equity_peak_timestamp  (when the peak was set — for decay)
  • cooling_until          (ISO timestamp when cooling period ends, or null)
  • current_exposure       (0.0 to 1.0 — fraction of equity allowed in positions)
  • last_rebalance         (ISO timestamp of last rebalance)
  • last_weights           (dict of symbol → target weight fraction)
  • daily_pnl              (dict of YYYY-MM-DD → pnl)

File location: state/strategy3_state.json (relative to project root).
"""

from __future__ import annotations
import json
import logging
import os
import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT      = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "state"
STATE_FILE = STATE_DIR / "strategy3_state.json"

DEFAULT_STATE: dict[str, Any] = {
    "equity_peak":           0.0,
    "equity_peak_timestamp": None,
    "cooling_until":         None,
    "current_exposure":      1.0,
    "last_rebalance":        None,
    "last_weights":          {},
    "daily_pnl":             {},
    "trades_today":          0,
    "last_trade_date":       None,
}


def _utcnow_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat().replace("+00:00", "Z"))


def load_state() -> dict:
    """Load state from disk, creating the file with defaults if missing."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        save_state(DEFAULT_STATE)
        return dict(DEFAULT_STATE)

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Merge missing keys from DEFAULT_STATE (forward compatibility)
        for k, v in DEFAULT_STATE.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        log.error("State file corrupted (%s) — resetting to defaults", e)
        save_state(DEFAULT_STATE)
        return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def reset_state() -> dict:
    """Wipe state back to defaults — used by reset_bot.bat or manual recovery."""
    save_state(DEFAULT_STATE)
    return dict(DEFAULT_STATE)


def get_state_path() -> Path:
    return STATE_FILE


def now_iso() -> str:
    return _utcnow_iso()
