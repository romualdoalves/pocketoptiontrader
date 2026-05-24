"""
strategy3_main.py — Orchestrator for the AI-Optimized Defensive Allocator.

Runs a loop every `interval_minutes` (default 5) that:
  1. Fetches bars for all core assets + hedge asset.
  2. Selects liquid universe + detects market regime.
  3. Runs LSTM alpha on each asset (if model available).
  4. Gates longs through the Binance macro filter.
  5. Computes hybrid allocation weights.
  6. Evaluates drawdown risk → scales exposure.
  7. Rebalances via Alpaca executor.
"""

from __future__ import annotations
import json
import logging
import os
import sys
import time
import datetime
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.strategy3.state import load_state, save_state, now_iso
from src.strategy3.indicators import compute_full_indicators
from src.strategy3.universe import select_universe
from src.strategy3.macro_filter import macro_allows_long
from src.strategy3.lstm_alpha import load_or_init, predict_signal, _TORCH_OK
from src.strategy3.allocator import compute_target_weights, needs_rebalance
from src.strategy3.risk_manager import evaluate_risk
from src.strategy3.alpaca_executor import AlpacaExecutor

log = logging.getLogger("strategy3")


# ---------------------------------------------------------------------------
# Bar fetching
# ---------------------------------------------------------------------------

def fetch_all_bars(
    symbols: list[str],
    timeframe: str,
    n_bars: int,
) -> Dict[str, pd.DataFrame]:
    """Fetch recent crypto bars for multiple symbols from Alpaca."""
    try:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    except ImportError:
        log.error("alpaca-py not installed — cannot fetch bars.")
        return {}

    unit = TimeFrameUnit.Minute if "Min" in timeframe else TimeFrameUnit.Hour
    amount = int("".join(c for c in timeframe if c.isdigit()) or 5)
    tf = TimeFrame(amount=amount, unit=unit)

    client = CryptoHistoricalDataClient()
    end   = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(minutes=amount * n_bars * 2)

    bars_by_symbol: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            req = CryptoBarsRequest(
                symbol_or_symbols=[sym], timeframe=tf,
                start=start, end=end,
            )
            bars = client.get_crypto_bars(req).df
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.xs(sym, level=0)
            bars = bars[["open", "high", "low", "close", "volume"]].tail(n_bars)
            if not bars.empty:
                bars_by_symbol[sym] = bars
        except Exception as e:
            log.warning("Failed to fetch bars for %s: %s", sym, e)
    return bars_by_symbol


# ---------------------------------------------------------------------------
# Single cycle
# ---------------------------------------------------------------------------

def run_cycle(cfg: dict, executor: AlpacaExecutor, state: dict) -> dict:
    """
    Execute one full strategy cycle. Returns updated state dict.
    """
    ucfg = cfg["universe"]
    rcfg = cfg["regime_detection"]
    lcfg = cfg["lstm"]
    mcfg = cfg["macro_filter"]
    acfg = cfg["allocator"]
    risk_cfg = cfg["risk_manager"]

    core_assets = ucfg["core_assets"]
    hedge_asset = ucfg["hedge_asset"]
    all_symbols = core_assets + [hedge_asset]
    timeframe   = ucfg["bar_timeframe"]
    n_bars      = ucfg["history_bars_for_indicators"]

    # --- 1. Fetch bars ---
    log.info("Fetching bars for %d symbols...", len(all_symbols))
    bars = fetch_all_bars(all_symbols, timeframe, n_bars)
    if not bars:
        log.error("No bars fetched — skipping cycle.")
        return state

    # --- 2. Compute indicators for each asset ---
    indicators: Dict[str, pd.DataFrame] = {}
    for sym, df in bars.items():
        try:
            indicators[sym] = compute_full_indicators(df)
        except Exception as e:
            log.warning("Indicator computation failed for %s: %s", sym, e)

    # --- 3. Universe selection + regime ---
    universe = select_universe(
        indicators, core_assets, hedge_asset, ucfg, rcfg,
    )
    liquid_core = universe["liquid_core"]
    hedge_w     = universe["hedge_weight"]
    regime      = universe["regime"]
    log.info("Regime: %s  Breadth: %s  Liquid: %s  Hedge weight: %.1f%%",
             regime["regime"], regime["breadth"], liquid_core, hedge_w * 100)

    # --- 4. LSTM alpha (if available) ---
    alpha_signals: Dict[str, int] = {}
    model, normalizer = load_or_init(
        str(ROOT / lcfg["model_path"]),
        str(ROOT / lcfg["normalizer_path"]),
        n_features=len(lcfg["features"]),
        hidden_size=lcfg["hidden_size"],
        num_layers=lcfg["num_layers"],
        dropout=lcfg["dropout"],
    )
    for sym in liquid_core:
        df = indicators.get(sym)
        if df is None:
            alpha_signals[sym] = 0
            continue
        sig = predict_signal(
            model, df, lcfg["features"], lcfg["sequence_length"],
            normalizer,
            lcfg["long_threshold"], lcfg["short_threshold"],
        )
        alpha_signals[sym] = sig["signal"]
        if sig["signal"] != 0:
            log.info("LSTM %s → %+d (pred=%.5f conf=%.2f)",
                     sym, sig["signal"], sig["pred"], sig["confidence"])

    # --- 5. Macro filter ---
    # Check if BTC price is rising (as proxy for broad market direction)
    btc_df = indicators.get("BTC/USD")
    price_rising = False
    if btc_df is not None and len(btc_df) >= 2:
        price_rising = btc_df["close"].iloc[-1] > btc_df["close"].iloc[-2]

    macro_ok, macro_reason, macro_raw = macro_allows_long(
        price_rising,
        mcfg["binance_symbol"],
        mcfg["funding_rate_threshold"],
        mcfg["oi_pct_threshold"],
    )
    if not macro_ok:
        log.warning("Macro filter blocking longs: %s", macro_reason)
        # Zero out all bullish signals
        for sym in list(alpha_signals.keys()):
            if alpha_signals[sym] > 0:
                alpha_signals[sym] = 0

    # --- 6. Compute target weights ---
    target_weights = compute_target_weights(
        indicators, liquid_core, hedge_asset, hedge_w, alpha_signals, acfg,
    )
    log.info("Target weights: %s", {s: f"{w:.3f}" for s, w in target_weights.items()})

    # --- 7. Check if rebalance needed ---
    current_weights = state.get("last_weights", {})
    threshold = acfg.get("rebalance_threshold_pct", 5.0)
    if not needs_rebalance(current_weights, target_weights, threshold):
        log.info("Weights within threshold — no rebalance needed.")
        return state

    # --- 8. Risk evaluation ---
    acct = executor.get_account()
    equity = acct["equity"]
    risk = evaluate_risk(equity, risk_cfg, state)
    exposure = risk["exposure"]
    log.info("Risk: DD=%.2f%%  Exposure=%.0f%%  Regime=%s",
             risk["dd_pct"], exposure * 100, risk["regime"])

    if exposure <= 0.0:
        log.warning("Exposure = 0 — going flat.")
        executor.cancel_all_open()
        # Close all positions except hedge
        positions = executor.get_positions()
        for sym in positions:
            if sym != hedge_asset:
                executor.close_position(sym)
        state["last_weights"] = {hedge_asset: 1.0}
        state["last_rebalance"] = now_iso()
        save_state(state)
        return state

    # --- 9. Execute rebalance ---
    log.info("Rebalancing with exposure=%.0f%%...", exposure * 100)
    executor.cancel_all_open()
    orders = executor.rebalance_to_targets(target_weights, equity, exposure)
    log.info("Submitted %d orders.", len(orders))

    state["last_weights"]   = target_weights
    state["last_rebalance"] = now_iso()
    save_state(state)

    return state


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    log_file = ROOT / "logs" / "strategy3.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(log_file), encoding="utf-8"),
        ],
    )

    config_path = ROOT / "config" / "strategy3_params.json"
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    dry_run = cfg["execution"].get("dry_run_default", True)
    if os.getenv("STRATEGY3_LIVE", "").lower() in ("1", "true", "yes"):
        dry_run = False

    executor = AlpacaExecutor(cfg["execution"], dry_run=dry_run)
    interval = cfg["scheduler"]["interval_minutes"] * 60

    log.info("=== Strategy 3 — AI-Optimized Defensive Allocator ===")
    log.info("Dry run: %s | Interval: %ds | Core: %s",
             dry_run, interval, cfg["universe"]["core_assets"])

    state = load_state()

    while True:
        try:
            cycle_start = time.monotonic()
            state = run_cycle(cfg, executor, state)
            elapsed = time.monotonic() - cycle_start
            log.info("Cycle completed in %.1fs", elapsed)
        except KeyboardInterrupt:
            log.info("Shutting down (KeyboardInterrupt).")
            break
        except Exception as e:
            log.exception("Cycle failed: %s", e)

        # Sleep until next interval
        remaining = max(0, interval - (time.monotonic() - cycle_start))
        if remaining > 0:
            log.info("Sleeping %.0fs until next cycle...", remaining)
            try:
                time.sleep(remaining)
            except KeyboardInterrupt:
                log.info("Shutting down (KeyboardInterrupt).")
                break


if __name__ == "__main__":
    main()
