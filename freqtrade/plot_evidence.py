"""
plot_evidence.py — Visual evidence generator for Strategy2 XAI layer.

Generates a 3-panel PNG for each closed trade:
  Panel 1 (top)    : OHLCV candlestick chart (last 50 bars) with entry/exit arrows,
                     VWAP, EMA50, POC/VAH/VAL lines, and Bollinger Bands.
  Panel 2 (middle) : Volume Delta bars (green = buying, red = selling).
  Panel 3 (bottom) : Z-Score line with ±2.0 threshold bands.

Saved to: /freqtrade/user_data/visual_proofs/<YYYYMMDD>_<pair>_<trade_key>.png
Images are compressed with Pillow to keep file size < 300 KB.

Called from explanation_engine.py in a daemon thread — never blocks the bot.

Can also be run standalone for backtesting review:
    python plot_evidence.py --json user_data/narratives/20260413_BTCUSD_*.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_BASE    = Path(os.environ.get("FREQTRADE_USER_DIR", "/freqtrade/user_data"))
VIZ_DIR  = _BASE / "visual_proofs"
VIZ_DIR.mkdir(parents=True, exist_ok=True)

# ── Chart configuration ───────────────────────────────────────────────────────
CHART_WIDTH_PX  = 1280
CHART_HEIGHT_PX = 800
PILLOW_QUALITY  = 85           # JPEG quality for thumbnail (PNG stays lossless)
CANDLES_SHOWN   = 50
DARK_BG         = "#0d1117"
GRID_COLOR      = "#21262d"
UP_COLOR        = "#3fb950"
DOWN_COLOR      = "#f85149"
NEUTRAL_COLOR   = "#8b949e"
ENTRY_COLOR     = "#58a6ff"
EXIT_COLOR      = "#f0883e"
VWAP_COLOR      = "#e3b341"
EMA_COLOR       = "#a371f7"
POC_COLOR       = "#ff7b72"


def generate_trade_chart(snap, df: pd.DataFrame) -> Path:
    """
    Main entry point called by ExplanationEngine.
    snap : TradeSnapshot (or dict with same fields)
    df   : DataFrame with all indicator columns (last N candles)
    Returns Path to the generated PNG file.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")               # non-interactive backend
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import matplotlib.gridspec as gridspec
    except ImportError:
        log.error("[plot_evidence] matplotlib not installed.")
        return Path("")

    # Normalise snap to dict
    if hasattr(snap, "__dict__") or hasattr(snap, "__dataclass_fields__"):
        from dataclasses import asdict
        snap_d = asdict(snap)
    elif isinstance(snap, dict):
        snap_d = snap
    else:
        snap_d = {}

    pair       = snap_d.get("pair", "BTC/USD")
    open_rate  = snap_d.get("open_rate", 0.0)
    close_rate = snap_d.get("close_rate", 0.0)
    poc        = snap_d.get("poc_at_entry", 0.0)
    vah        = snap_d.get("vah_at_entry", 0.0)
    val        = snap_d.get("val_at_entry", 0.0)
    stop_price = snap_d.get("structural_stop", 0.0)
    target     = snap_d.get("liquidity_target", 0.0)
    trade_key  = snap_d.get("trade_key", "trade")
    open_date  = snap_d.get("open_date", "")

    # ── Trim to last CANDLES_SHOWN bars ──────────────────────────────────────
    if df.empty or len(df) < 5:
        log.warning("[plot_evidence] Not enough bars to plot (%d).", len(df))
        return Path("")

    plot_df = df.tail(CANDLES_SHOWN).reset_index(drop=True)
    n       = len(plot_df)
    x       = np.arange(n)

    # ── Figure layout ─────────────────────────────────────────────────────────
    dpi = 100
    fig = plt.figure(
        figsize=(CHART_WIDTH_PX / dpi, CHART_HEIGHT_PX / dpi),
        dpi=dpi, facecolor=DARK_BG
    )
    gs  = gridspec.GridSpec(3, 1, figure=fig,
                             height_ratios=[3, 1, 1],
                             hspace=0.08)
    ax1 = fig.add_subplot(gs[0])   # candlestick
    ax2 = fig.add_subplot(gs[1], sharex=ax1)   # volume delta
    ax3 = fig.add_subplot(gs[2], sharex=ax1)   # z-score

    for ax in (ax1, ax2, ax3):
        ax.set_facecolor(DARK_BG)
        ax.tick_params(colors=NEUTRAL_COLOR, labelsize=8)
        ax.spines["bottom"].set_color(GRID_COLOR)
        ax.spines["top"].set_color(GRID_COLOR)
        ax.spines["left"].set_color(GRID_COLOR)
        ax.spines["right"].set_color(GRID_COLOR)
        ax.yaxis.label.set_color(NEUTRAL_COLOR)
        ax.xaxis.label.set_color(NEUTRAL_COLOR)
        ax.grid(color=GRID_COLOR, linewidth=0.5, linestyle="--", alpha=0.6)

    # ── Panel 1: Candlesticks ─────────────────────────────────────────────────
    candle_width = 0.6
    for i, row in plot_df.iterrows():
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        color = UP_COLOR if c >= o else DOWN_COLOR
        ax1.plot([i, i], [l, h], color=color, linewidth=0.8, alpha=0.9)
        ax1.add_patch(mpatches.FancyBboxPatch(
            (i - candle_width / 2, min(o, c)),
            candle_width, abs(c - o),
            boxstyle="square,pad=0",
            facecolor=color, edgecolor=color, alpha=0.85
        ))

    # ── Overlay indicators ────────────────────────────────────────────────────
    def _plot_line(col, color, label, lw=1.0, ls="-"):
        if col in plot_df.columns:
            ax1.plot(x, plot_df[col].values, color=color,
                     linewidth=lw, linestyle=ls, label=label, alpha=0.85)

    _plot_line("vwap",         VWAP_COLOR, "VWAP",      lw=1.2)
    _plot_line("ema50",        EMA_COLOR,  "EMA50",     lw=1.0, ls="--")
    _plot_line("bb_upperband", NEUTRAL_COLOR, "BB",     lw=0.8, ls=":")
    _plot_line("bb_lowerband", NEUTRAL_COLOR, "",       lw=0.8, ls=":")

    # Volume profile levels
    for price, color, label in [
        (poc, POC_COLOR,   "POC"),
        (vah, "#79c0ff",   "VAH"),
        (val, "#ffa657",   "VAL"),
    ]:
        if price:
            ax1.axhline(price, color=color, linewidth=1.0,
                        linestyle="--", alpha=0.7, label=label)

    # Stop / target dashed lines
    if stop_price:
        ax1.axhline(stop_price, color=DOWN_COLOR, linewidth=1.2,
                    linestyle=":", alpha=0.8, label="Stop")
    if target:
        ax1.axhline(target, color=UP_COLOR, linewidth=1.2,
                    linestyle=":", alpha=0.8, label="Target")

    # ── Entry / Exit arrows ───────────────────────────────────────────────────
    # Find approximate x positions for open/close bars
    entry_x = _find_bar_index(plot_df, open_date) if open_date else n - 5
    exit_x  = n - 1  # exit is always the last bar in the window

    if open_rate:
        ax1.annotate(
            f"  ENTRY\n  ${open_rate:,.0f}",
            xy=(entry_x, open_rate),
            xytext=(entry_x - 4, open_rate * 0.998),
            color=ENTRY_COLOR, fontsize=7, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=ENTRY_COLOR, lw=1.2),
        )
        ax1.scatter([entry_x], [open_rate], color=ENTRY_COLOR,
                    marker="^", s=80, zorder=5)

    if close_rate:
        pnl = snap_d.get("close_profit_pct", 0.0)
        ax1.annotate(
            f"  EXIT {'+' if pnl >= 0 else ''}{pnl:.2f}%\n  ${close_rate:,.0f}",
            xy=(exit_x, close_rate),
            xytext=(exit_x - 4, close_rate * 1.002),
            color=EXIT_COLOR, fontsize=7, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=EXIT_COLOR, lw=1.2),
        )
        ax1.scatter([exit_x], [close_rate], color=EXIT_COLOR,
                    marker="v", s=80, zorder=5)

    # Legend + title
    ax1.legend(loc="upper left", fontsize=7, facecolor=DARK_BG,
               labelcolor=NEUTRAL_COLOR, framealpha=0.8, ncol=4)
    pnl_pct  = snap_d.get("close_profit_pct", 0.0)
    pnl_usd  = snap_d.get("close_profit_abs", 0.0)
    result   = "WIN" if pnl_pct >= 0 else "LOSS"
    pnl_sign = "+" if pnl_pct >= 0 else ""
    ax1.set_title(
        f"Strategy2 Liquidity Sentinel | {pair} | {result}  "
        f"{pnl_sign}{pnl_pct:.3f}%  ({pnl_sign}${pnl_usd:.2f})  |  "
        f"Regime: {snap_d.get('regime','?')}  ADX={snap_d.get('adx_at_entry',0):.1f}",
        color="white", fontsize=9, fontweight="bold", pad=6
    )
    ax1.set_ylabel("Price (USD)", color=NEUTRAL_COLOR, fontsize=8)
    ax1.tick_params(labelbottom=False)

    # ── Panel 2: Volume Delta ─────────────────────────────────────────────────
    if "volume_delta" in plot_df.columns:
        vd    = plot_df["volume_delta"].values
        colors = [UP_COLOR if v >= 0 else DOWN_COLOR for v in vd]
        ax2.bar(x, vd, color=colors, alpha=0.75, width=0.7)
    elif "volume" in plot_df.columns:
        # fallback: plain volume coloured by candle direction
        vol    = plot_df["volume"].values
        dirs   = (plot_df["close"] >= plot_df["open"]).values
        colors = [UP_COLOR if d else DOWN_COLOR for d in dirs]
        ax2.bar(x, vol, color=colors, alpha=0.75, width=0.7)

    ax2.axhline(0, color=NEUTRAL_COLOR, linewidth=0.5)
    ax2.set_ylabel("Vol Delta", color=NEUTRAL_COLOR, fontsize=7)
    ax2.tick_params(labelbottom=False)

    # ── Panel 3: Z-Score ──────────────────────────────────────────────────────
    if "delta_z" in plot_df.columns:
        z = plot_df["delta_z"].values
        ax3.plot(x, z, color=ENTRY_COLOR, linewidth=1.2, label="Z-Score")
        ax3.axhline( 2.0, color=UP_COLOR,   linewidth=0.8, linestyle="--", alpha=0.7)
        ax3.axhline(-2.0, color=DOWN_COLOR, linewidth=0.8, linestyle="--", alpha=0.7)
        ax3.axhline( 0,   color=NEUTRAL_COLOR, linewidth=0.5)
        ax3.fill_between(x, z, 0,
                          where=(z > 0), color=UP_COLOR,   alpha=0.12)
        ax3.fill_between(x, z, 0,
                          where=(z < 0), color=DOWN_COLOR, alpha=0.12)
        ax3.set_ylim(-5, 5)
        ax3.legend(loc="upper left", fontsize=7, facecolor=DARK_BG,
                   labelcolor=NEUTRAL_COLOR, framealpha=0.8)

    ax3.set_ylabel("Z-Score", color=NEUTRAL_COLOR, fontsize=7)

    # X-axis ticks — show time labels every ~10 bars
    tick_step = max(1, n // 8)
    tick_pos  = x[::tick_step]
    if "date" in plot_df.columns:
        tick_lbl = [
            pd.to_datetime(plot_df["date"].iloc[i]).strftime("%m/%d %H:%M")
            for i in tick_pos
        ]
    else:
        tick_lbl = [str(i) for i in tick_pos]

    ax3.set_xticks(tick_pos)
    ax3.set_xticklabels(tick_lbl, rotation=30, ha="right",
                         color=NEUTRAL_COLOR, fontsize=7)

    # ── Watermark ─────────────────────────────────────────────────────────────
    fig.text(0.99, 0.01, "AlpacaTrader | Strategy2 XAI",
             color=NEUTRAL_COLOR, fontsize=6, ha="right", va="bottom", alpha=0.5)

    # ── Save PNG → optimise with Pillow ──────────────────────────────────────
    date_prefix = (open_date[:10] if open_date else "0000-00-00").replace("-", "")
    safe_pair   = pair.replace("/", "")
    filename    = f"{date_prefix}_{safe_pair}_{trade_key}.png"
    out_path    = VIZ_DIR / filename

    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=dpi, bbox_inches="tight",
                facecolor=DARK_BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)

    try:
        from PIL import Image
        img = Image.open(buf)
        # Resize to 1280×800 max, keep aspect ratio
        img.thumbnail((CHART_WIDTH_PX, CHART_HEIGHT_PX), Image.LANCZOS)
        img.save(out_path, "PNG", optimize=True, compress_level=6)
        log.info("[plot_evidence] Chart saved (%d KB): %s",
                 out_path.stat().st_size // 1024, out_path)
    except ImportError:
        # Pillow not installed — save raw matplotlib output
        with open(out_path, "wb") as f:
            f.write(buf.getvalue())
        log.info("[plot_evidence] Chart saved (no Pillow): %s", out_path)
    except Exception as e:
        log.warning("[plot_evidence] Pillow error: %s", e)
        with open(out_path, "wb") as f:
            buf.seek(0)
            f.write(buf.getvalue())

    return out_path


def _find_bar_index(df: pd.DataFrame, iso_date: str) -> int:
    """Find the dataframe row index closest to the given ISO timestamp."""
    if "date" not in df.columns:
        return max(0, len(df) - 10)
    try:
        target = pd.to_datetime(iso_date, utc=True)
        diffs  = (pd.to_datetime(df["date"], utc=True) - target).abs()
        return int(diffs.idxmin())
    except Exception:
        return max(0, len(df) - 10)


# ── Standalone CLI ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-generate XAI charts from JSON narratives")
    parser.add_argument("--json", nargs="+", required=True,
                        help="Path(s) to narrative JSON files")
    parser.add_argument("--csv",  default=None,
                        help="Optional path to OHLCV CSV for this pair/date")
    args = parser.parse_args()

    for jpath in args.json:
        with open(jpath) as f:
            snap = json.load(f)

        if args.csv:
            df = pd.read_csv(args.csv, parse_dates=["date"])
        else:
            df = pd.DataFrame()

        out = generate_trade_chart(snap, df)
        print(f"Generated: {out}")
