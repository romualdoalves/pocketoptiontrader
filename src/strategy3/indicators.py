"""
indicators.py — Technical indicator computations for Strategy 3.

All functions take a pandas DataFrame with columns: open, high, low, close, volume
and return the input DataFrame with new columns appended.

Vectorized numpy/pandas only — no talib dependency.
"""

from __future__ import annotations
import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    out   = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def vwap(df: pd.DataFrame) -> pd.Series:
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_pv = (typical_price * df["volume"]).cumsum()
    cum_v  = df["volume"].cumsum().replace(0, np.nan)
    return (cum_pv / cum_v).ffill().fillna(df["close"])


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c  = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def compute_full_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute all indicators needed by Strategy 3 and the LSTM feature pipeline.
    Adds columns: ema20, ema50, ema200, rsi, macd, macd_signal, macd_hist,
                  vwap, atr, log_return, volume_ratio, ema_ratio, vwap_ratio,
                  atr_ratio, hour_sin, hour_cos, dow_sin
    """
    out = df.copy()

    # Core indicators
    out["ema20"]  = ema(out["close"], 20)
    out["ema50"]  = ema(out["close"], 50)
    out["ema200"] = ema(out["close"], 200)
    out["rsi"]    = rsi(out["close"], 14)
    macd_line, macd_sig, macd_hist = macd(out["close"])
    out["macd"]        = macd_line
    out["macd_signal"] = macd_sig
    out["macd_hist"]   = macd_hist
    out["vwap"]        = vwap(out)
    out["atr"]         = atr(out, 14)

    # Derived ratios (LSTM features)
    out["log_return"]   = np.log(out["close"] / out["close"].shift(1)).fillna(0)
    vol_mean            = out["volume"].rolling(50, min_periods=1).mean()
    out["volume_ratio"] = (out["volume"] / vol_mean.replace(0, np.nan)).fillna(1.0)
    out["ema_ratio"]    = (out["close"] / out["ema50"].replace(0, np.nan)).fillna(1.0) - 1.0
    out["vwap_ratio"]   = (out["close"] / out["vwap"].replace(0, np.nan)).fillna(1.0) - 1.0
    out["atr_ratio"]    = (out["atr"] / out["close"].replace(0, np.nan)).fillna(0)

    # Cyclical time encodings (from the bar timestamp)
    if "timestamp" in out.columns or isinstance(out.index, pd.DatetimeIndex):
        ts = out.index if isinstance(out.index, pd.DatetimeIndex) else pd.to_datetime(out["timestamp"])
        hours = ts.hour.values if hasattr(ts, "hour") else pd.to_datetime(ts).dt.hour.values
        dows  = ts.dayofweek.values if hasattr(ts, "dayofweek") else pd.to_datetime(ts).dt.dayofweek.values
        out["hour_sin"] = np.sin(2 * np.pi * hours / 24.0)
        out["hour_cos"] = np.cos(2 * np.pi * hours / 24.0)
        out["dow_sin"]  = np.sin(2 * np.pi * dows / 7.0)
    else:
        out["hour_sin"] = 0.0
        out["hour_cos"] = 0.0
        out["dow_sin"]  = 0.0

    return out
