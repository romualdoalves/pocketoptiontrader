"""
explanation_engine.py — XAI (Explainability) layer for Strategy2 Liquidity Sentinel.

Captures the bot's "state of mind" at every trade entry and exit, generates
YouTube-ready narrative text, persists to local JSON files (resilient) and
asynchronously syncs to BigQuery.

Public API used by strategy2_freqtrade.py:
    engine = ExplanationEngine()

    # Called from confirm_trade_entry (after gates pass)
    engine.capture_entry(df, pair, rate, entry_tag, current_time)

    # Called from confirm_trade_exit
    engine.capture_exit(trade, df, rate, exit_reason, current_profit, current_time)

Outputs per trade:
    /freqtrade/user_data/narratives/<YYYYMMDD>_<pair>_<trade_id>.json
    BigQuery table: alpacatrader.trade_narratives
    Freqtrade log line (INFO level)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_BASE      = Path(os.environ.get("FREQTRADE_USER_DIR", "/freqtrade/user_data"))
NARRATIVES = _BASE / "narratives"
VIZ_DIR    = _BASE / "visual_proofs"

NARRATIVES.mkdir(parents=True, exist_ok=True)
VIZ_DIR.mkdir(parents=True, exist_ok=True)

# ── BigQuery (optional — fails gracefully) ────────────────────────────────────
_BQ_PROJECT = os.environ.get("GCP_PROJECT_ID", "")
_BQ_DATASET = os.environ.get("BQ_DATASET", "alpacatrader")
_BQ_TABLE   = "trade_narratives"

BQ_SCHEMA = [
    ("trade_key",           "STRING"),    # pair + open_date slug — primary key
    ("pair",                "STRING"),
    ("open_date",           "TIMESTAMP"),
    ("close_date",          "TIMESTAMP"),
    ("open_rate",           "FLOAT"),
    ("close_rate",          "FLOAT"),
    ("close_profit_pct",    "FLOAT"),
    ("close_profit_abs",    "FLOAT"),
    ("exit_reason",         "STRING"),
    ("entry_tag",           "STRING"),
    ("regime",              "STRING"),
    ("adx_at_entry",        "FLOAT"),
    ("z_score_at_entry",    "FLOAT"),
    ("mfi_at_entry",        "FLOAT"),
    ("vwap_distance_pct",   "FLOAT"),
    ("ema50_slope",         "FLOAT"),
    ("poc_at_entry",        "FLOAT"),
    ("vah_at_entry",        "FLOAT"),
    ("val_at_entry",        "FLOAT"),
    ("bb_width_at_entry",   "FLOAT"),
    ("structural_stop",     "FLOAT"),
    ("liquidity_target",    "FLOAT"),
    ("funding_rate",        "FLOAT"),
    ("narrative_text",      "STRING"),
    ("chart_path",          "STRING"),
    ("synced_at",           "TIMESTAMP"),
]


# ── Data container ─────────────────────────────────────────────────────────────
@dataclass
class TradeSnapshot:
    # Identity
    pair:              str     = ""
    trade_key:         str     = ""          # pair + open_date slug
    entry_tag:         str     = ""

    # Prices
    open_rate:         float   = 0.0
    close_rate:        float   = 0.0
    structural_stop:   float   = 0.0
    liquidity_target:  float   = 0.0

    # P&L (filled on exit)
    close_profit_pct:  float   = 0.0
    close_profit_abs:  float   = 0.0
    exit_reason:       str     = ""

    # Timestamps (ISO strings for JSON serialisation)
    open_date:         str     = ""
    close_date:        str     = ""

    # Indicator state at entry
    regime:            str     = ""
    adx_at_entry:      float   = 0.0
    z_score_at_entry:  float   = 0.0
    mfi_at_entry:      float   = 0.0
    vwap_distance_pct: float   = 0.0   # (price - vwap) / vwap * 100
    ema50_slope:       float   = 0.0   # (ema50[-1] - ema50[-3]) / ema50[-3] * 100
    poc_at_entry:      float   = 0.0
    vah_at_entry:      float   = 0.0
    val_at_entry:      float   = 0.0
    bb_width_at_entry: float   = 0.0   # (bb_upper - bb_lower) / bb_mid * 100
    funding_rate:      float   = 0.0

    # Output
    narrative_text:    str     = ""
    chart_path:        str     = ""
    synced_at:         str     = ""


# ── Engine class ───────────────────────────────────────────────────────────────
class ExplanationEngine:
    """
    Thread-safe explanation layer.  One instance per strategy class is enough.
    All I/O (BigQuery, chart generation) runs in daemon threads so the trading
    loop is never blocked.
    """

    def __init__(self):
        self._pending: dict[str, TradeSnapshot] = {}   # trade_key → snapshot
        self._lock = threading.Lock()

    # ── Entry capture ─────────────────────────────────────────────────────────
    def capture_entry(
        self,
        df: pd.DataFrame,
        pair: str,
        rate: float,
        entry_tag: str,
        current_time: datetime,
    ) -> TradeSnapshot:
        """
        Called from confirm_trade_entry.
        Extracts all indicator values from the last analysed row of df.
        Returns the snapshot (stored internally keyed by trade_key).
        """
        snap = TradeSnapshot()
        snap.pair      = pair
        snap.open_rate = rate
        snap.open_date = current_time.astimezone(timezone.utc).isoformat()
        snap.entry_tag = entry_tag or "unknown"
        snap.trade_key = _make_key(pair, snap.open_date)

        last = df.iloc[-1] if not df.empty else pd.Series(dtype=float)

        snap.adx_at_entry      = _safe(last, "adx")
        snap.z_score_at_entry  = _safe(last, "delta_z")
        snap.mfi_at_entry      = _safe(last, "mfi")
        snap.poc_at_entry      = _safe(last, "poc")
        snap.vah_at_entry      = _safe(last, "vah")
        snap.val_at_entry      = _safe(last, "val")
        snap.funding_rate      = _safe(last, "funding_rate")

        vwap = _safe(last, "vwap")
        snap.vwap_distance_pct = ((rate - vwap) / vwap * 100) if vwap else 0.0

        # EMA50 slope: (ema[-1] - ema[-4]) / ema[-4] * 100  (3-bar change)
        if len(df) >= 4:
            ema_now  = _safe(df.iloc[-1], "ema50")
            ema_prev = _safe(df.iloc[-4], "ema50")
            snap.ema50_slope = ((ema_now - ema_prev) / ema_prev * 100) if ema_prev else 0.0

        bb_upper = _safe(last, "bb_upperband")
        bb_lower = _safe(last, "bb_lowerband")
        bb_mid   = _safe(last, "bb_middleband")
        if bb_mid:
            snap.bb_width_at_entry = (bb_upper - bb_lower) / bb_mid * 100

        # Detect regime from entry_tag label
        if "trending" in (entry_tag or "").lower():
            snap.regime = "TRENDING"
        elif "reversion" in (entry_tag or "").lower():
            snap.regime = "RANGING"
        else:
            snap.regime = "TRENDING" if snap.adx_at_entry > 25 else "RANGING"

        # Risk levels (same formulas as strategy)
        atr = _safe(last, "atr")
        snap.structural_stop   = snap.poc_at_entry - 1.5 * atr
        snap.liquidity_target  = rate + 3.0 * atr

        with self._lock:
            self._pending[snap.trade_key] = snap

        log.info("[XAI] Entry snapshot captured for %s | regime=%s | ADX=%.1f | "
                 "Z=%.2f | VWAP_dist=%.2f%%",
                 pair, snap.regime, snap.adx_at_entry,
                 snap.z_score_at_entry, snap.vwap_distance_pct)
        return snap

    # ── Exit capture ──────────────────────────────────────────────────────────
    def capture_exit(
        self,
        trade,                        # freqtrade Trade ORM object
        df: pd.DataFrame,
        rate: float,
        exit_reason: str,
        current_profit: float,
        current_time: datetime,
    ) -> Optional[TradeSnapshot]:
        """
        Called from confirm_trade_exit.
        Completes the snapshot, generates the narrative, and fires async tasks.
        """
        open_date_str = trade.open_date_utc.isoformat()
        trade_key     = _make_key(trade.pair, open_date_str)

        with self._lock:
            snap = self._pending.pop(trade_key, None)

        if snap is None:
            # Entry was before this session — reconstruct minimal snapshot
            snap = TradeSnapshot()
            snap.pair      = trade.pair
            snap.open_rate = trade.open_rate
            snap.open_date = open_date_str
            snap.trade_key = trade_key
            snap.regime    = "UNKNOWN"

        snap.close_rate       = rate
        snap.close_date       = current_time.astimezone(timezone.utc).isoformat()
        snap.exit_reason      = exit_reason or "unknown"
        snap.close_profit_pct = current_profit * 100
        snap.close_profit_abs = trade.stake_amount * current_profit

        # Generate narrative
        snap.narrative_text = self._build_narrative(snap)
        snap.synced_at      = datetime.now(timezone.utc).isoformat()

        log.info("[XAI] %s", snap.narrative_text)

        # Async: save JSON + push to BQ + generate chart
        threading.Thread(
            target=self._async_persist,
            args=(snap, df.copy() if df is not None else pd.DataFrame()),
            daemon=True,
        ).start()

        return snap

    # ── Narrative generator ───────────────────────────────────────────────────
    @staticmethod
    def _build_narrative(snap: TradeSnapshot) -> str:
        """
        Builds a YouTube-ready executive summary.
        Template (English + Portuguese bilingual block):
        """
        direction  = "LONG"
        pnl_sign   = "+" if snap.close_profit_pct >= 0 else ""
        result_tag = "WIN" if snap.close_profit_pct >= 0 else "LOSS"
        entry_engine = snap.entry_tag.upper() if snap.entry_tag else "AUTO"

        en = (
            f"[Strategy2 Analysis | {result_tag}] "
            f"{direction} entry on {snap.pair} "
            f"motivated by {snap.regime} regime (Engine: {entry_engine}). "
            f"Volume Delta Z-Score reached {snap.z_score_at_entry:.2f}, "
            f"validating institutional aggression. "
            f"ADX={snap.adx_at_entry:.1f} | "
            f"MFI={snap.mfi_at_entry:.1f} | "
            f"VWAP distance={snap.vwap_distance_pct:.2f}% | "
            f"EMA50 slope={snap.ema50_slope:.3f}%. "
            f"Stop Loss at {snap.structural_stop:.2f} (structural: POC - 1.5xATR). "
            f"Liquidity Target: {snap.liquidity_target:.2f} (entry + 3.0xATR). "
            f"Exit reason: {snap.exit_reason}. "
            f"Result: {pnl_sign}{snap.close_profit_pct:.3f}% "
            f"({pnl_sign}${snap.close_profit_abs:.2f} USD)."
        )

        pt = (
            f"[Analise Strategy2 | {result_tag}] "
            f"Entrada {direction} em {snap.pair} "
            f"motivada por regime {snap.regime} (Motor: {entry_engine}). "
            f"Z-Score de Volume atingiu {snap.z_score_at_entry:.2f}, "
            f"validando agressividade institucional. "
            f"ADX={snap.adx_at_entry:.1f} | "
            f"MFI={snap.mfi_at_entry:.1f} | "
            f"distancia VWAP={snap.vwap_distance_pct:.2f}% | "
            f"inclinacao EMA50={snap.ema50_slope:.3f}%. "
            f"Stop Loss em {snap.structural_stop:.2f} via ATR. "
            f"Alvo de liquidez: {snap.liquidity_target:.2f}. "
            f"Razao de saida: {snap.exit_reason}. "
            f"Resultado: {pnl_sign}{snap.close_profit_pct:.3f}% "
            f"({pnl_sign}${snap.close_profit_abs:.2f} USD)."
        )

        return f"{en}\n---PT---\n{pt}"

    # ── Async persist ─────────────────────────────────────────────────────────
    def _async_persist(self, snap: TradeSnapshot, df: pd.DataFrame):
        """Runs in a daemon thread: save JSON → generate chart → push to BQ."""
        # 1. Save JSON narrative
        _save_json(snap)

        # 2. Generate chart (imports are inside to avoid slowing strategy import)
        try:
            import sys as _sys
            from pathlib import Path as _Path
            # plot_evidence.py lives one level up (freqtrade/plot_evidence.py)
            # Add both the strategies folder and its parent to sys.path
            _strat_dir = _Path(__file__).parent
            for _p in [str(_strat_dir), str(_strat_dir.parent),
                       "/freqtrade", "/app"]:
                if _p not in _sys.path:
                    _sys.path.insert(0, _p)
            from plot_evidence import generate_trade_chart
            chart_path = generate_trade_chart(snap, df)
            snap.chart_path = str(chart_path)
            _save_json(snap)   # re-save with chart path
        except Exception as e:
            log.warning("[XAI] Chart generation failed: %s", e)

        # 3. Push to BigQuery
        _push_to_bq(snap)


# ── Helpers ────────────────────────────────────────────────────────────────────
def _make_key(pair: str, open_date_iso: str) -> str:
    safe_pair = pair.replace("/", "_")
    ts_slug   = open_date_iso[:19].replace(":", "").replace("-", "").replace("T", "_")
    return f"{safe_pair}_{ts_slug}"


def _safe(row, col: str, default: float = 0.0) -> float:
    try:
        v = row[col]
        return float(v) if (v is not None and not np.isnan(float(v))) else default
    except Exception:
        return default


def _save_json(snap: TradeSnapshot):
    date_prefix = snap.open_date[:10].replace("-", "")
    filename    = f"{date_prefix}_{snap.trade_key}.json"
    path        = NARRATIVES / filename
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(snap), f, indent=2, default=str)
        log.debug("[XAI] Snapshot saved to %s", path)
    except Exception as e:
        log.warning("[XAI] Failed to save JSON snapshot: %s", e)


def _push_to_bq(snap: TradeSnapshot):
    if not _BQ_PROJECT:
        log.debug("[XAI] GCP_PROJECT_ID not set — skipping BigQuery push.")
        return
    try:
        from google.cloud import bigquery  # lazy import
        client   = bigquery.Client(project=_BQ_PROJECT)
        full_id  = f"{_BQ_PROJECT}.{_BQ_DATASET}.{_BQ_TABLE}"

        # Ensure table exists
        try:
            client.get_table(full_id)
        except Exception:
            schema = [bigquery.SchemaField(n, t) for n, t in BQ_SCHEMA]
            client.create_table(bigquery.Table(full_id, schema=schema))
            log.info("[XAI] Created BigQuery table %s", full_id)

        row = asdict(snap)
        # Convert ISO strings to BigQuery TIMESTAMP strings
        for col in ("open_date", "close_date", "synced_at"):
            if row.get(col):
                row[col] = row[col].replace("+00:00", "").replace("Z", "")

        errors = client.insert_rows_json(full_id, [row])
        if errors:
            log.warning("[XAI] BigQuery insert errors: %s", errors)
        else:
            log.info("[XAI] Snapshot pushed to BigQuery for %s", snap.trade_key)
    except Exception as e:
        log.warning("[XAI] BigQuery push failed: %s", e)
