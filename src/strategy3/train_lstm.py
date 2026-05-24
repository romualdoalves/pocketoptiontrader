"""
train_lstm.py — Offline training pipeline for the Strategy 3 LSTM alpha model.

Usage:
    python -m src.strategy3.train_lstm --symbol BTC/USD [--epochs 30]

Pulls historical bars from Alpaca, computes features via indicators.compute_full_indicators,
fits a per-feature normalizer, trains an LSTM to predict the log return `target_horizon_bars`
into the future, then saves model weights + normalizer JSON.

Torch is required for this script; if it's missing we exit with an error
(unlike lstm_alpha.py which silently degrades at inference time).
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger("train_lstm")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.strategy3.indicators import compute_full_indicators        # noqa: E402
from src.strategy3.lstm_alpha import LSTMModel, Normalizer, _TORCH_OK  # noqa: E402

if not _TORCH_OK:
    print("ERROR: PyTorch is required for training. Install with `pip install torch`.")
    sys.exit(1)

import torch                                                         # noqa: E402
import torch.nn as nn                                                # noqa: E402
from torch.utils.data import DataLoader, TensorDataset               # noqa: E402


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_bars(symbol: str, n_bars: int, timeframe: str = "5Min") -> pd.DataFrame:
    """
    Fetch historical crypto bars from Alpaca. Requires ALPACA_API_KEY/SECRET env vars.
    Returns a DataFrame indexed by timestamp with columns open/high/low/close/volume.
    """
    try:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    except ImportError:
        raise RuntimeError("alpaca-py not installed. `pip install alpaca-py`")

    unit = TimeFrameUnit.Minute if "Min" in timeframe else TimeFrameUnit.Hour
    amount = int("".join(c for c in timeframe if c.isdigit()) or 5)
    tf = TimeFrame(amount=amount, unit=unit)

    client = CryptoHistoricalDataClient()  # public endpoints — no keys needed for crypto
    import datetime as dt
    end   = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(minutes=amount * n_bars * 2)

    req = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=tf, start=start, end=end)
    bars = client.get_crypto_bars(req).df

    if bars.empty:
        raise RuntimeError(f"No bars returned for {symbol}")

    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(symbol, level=0)

    bars = bars[["open", "high", "low", "close", "volume"]].copy()
    bars = bars.tail(n_bars)
    log.info("Fetched %d bars for %s", len(bars), symbol)
    return bars


# ---------------------------------------------------------------------------
# Supervised dataset builder
# ---------------------------------------------------------------------------

def build_sequences(
    df: pd.DataFrame,
    features: List[str],
    sequence_length: int,
    target_horizon: int,
    normalizer: Normalizer,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Construct (X, y) pairs.
    X[i] = feature window [i .. i+seq_len)
    y[i] = log(close[i+seq_len+horizon-1] / close[i+seq_len-1])
    """
    arr = normalizer.transform(df, features)                # (T, F)
    closes = df["close"].values
    T = len(df)

    X_list, y_list = [], []
    last = T - sequence_length - target_horizon
    for i in range(last):
        X_list.append(arr[i : i + sequence_length])
        c0 = closes[i + sequence_length - 1]
        c1 = closes[i + sequence_length - 1 + target_horizon]
        y_list.append(np.log(c1 / c0) if c0 > 0 else 0.0)

    X = np.asarray(X_list, dtype=np.float32)
    y = np.asarray(y_list, dtype=np.float32)
    return X, y


# ---------------------------------------------------------------------------
# Training loop
# ---------------------------------------------------------------------------

def train(
    X: np.ndarray,
    y: np.ndarray,
    n_features: int,
    hidden_size: int,
    num_layers: int,
    dropout: float,
    val_split: float,
    batch_size: int,
    epochs: int,
    lr: float,
    weight_decay: float,
    early_stop_patience: int,
    device: str = "cpu",
) -> LSTMModel:
    n = len(X)
    n_val = max(1, int(n * val_split))
    n_tr  = n - n_val

    X_tr, y_tr = torch.from_numpy(X[:n_tr]), torch.from_numpy(y[:n_tr])
    X_va, y_va = torch.from_numpy(X[n_tr:]), torch.from_numpy(y[n_tr:])

    tr_loader = DataLoader(TensorDataset(X_tr, y_tr), batch_size=batch_size, shuffle=True)
    va_loader = DataLoader(TensorDataset(X_va, y_va), batch_size=batch_size, shuffle=False)

    model = LSTMModel(n_features, hidden_size, num_layers, dropout).to(device)
    opt   = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)
    loss_fn = nn.MSELoss()

    best_val = float("inf")
    patience = 0
    best_state = None

    for ep in range(1, epochs + 1):
        model.train()
        tr_loss = 0.0
        for xb, yb in tr_loader:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            pred = model(xb)
            loss = loss_fn(pred, yb)
            loss.backward()
            opt.step()
            tr_loss += loss.item() * len(xb)
        tr_loss /= max(1, n_tr)

        model.eval()
        va_loss = 0.0
        with torch.no_grad():
            for xb, yb in va_loader:
                xb, yb = xb.to(device), yb.to(device)
                va_loss += loss_fn(model(xb), yb).item() * len(xb)
        va_loss /= max(1, n_val)

        log.info("epoch %02d  train=%.6f  val=%.6f", ep, tr_loss, va_loss)

        if va_loss < best_val - 1e-7:
            best_val = va_loss
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            patience = 0
        else:
            patience += 1
            if patience >= early_stop_patience:
                log.info("early stop at epoch %d (best val=%.6f)", ep, best_val)
                break

    if best_state is not None:
        model.load_state_dict(best_state)
    return model


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol",    default="BTC/USD")
    ap.add_argument("--config",    default=str(ROOT / "config" / "strategy3_params.json"))
    ap.add_argument("--epochs",    type=int, default=None)
    ap.add_argument("--device",    default="cpu")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    lcfg = cfg["lstm"]
    tcfg = cfg["training"]
    ucfg = cfg["universe"]

    features        = lcfg["features"]
    seq_len         = int(lcfg["sequence_length"])
    hidden_size     = int(lcfg["hidden_size"])
    num_layers      = int(lcfg["num_layers"])
    dropout         = float(lcfg["dropout"])
    model_path      = ROOT / lcfg["model_path"]
    normalizer_path = ROOT / lcfg["normalizer_path"]

    n_bars          = int(tcfg["train_bars"])
    val_split       = float(tcfg["val_split"])
    batch_size      = int(tcfg["batch_size"])
    epochs          = int(args.epochs or tcfg["epochs"])
    lr              = float(tcfg["learning_rate"])
    wd              = float(tcfg["weight_decay"])
    patience        = int(tcfg["early_stop_patience"])
    horizon         = int(tcfg["target_horizon_bars"])

    bars = fetch_bars(args.symbol, n_bars, ucfg["bar_timeframe"])
    df   = compute_full_indicators(bars).dropna().reset_index(drop=True)
    log.info("After indicators/dropna: %d rows", len(df))

    normalizer = Normalizer.fit(df, features)
    X, y       = build_sequences(df, features, seq_len, horizon, normalizer)
    log.info("Dataset: X=%s  y=%s", X.shape, y.shape)

    model = train(
        X, y,
        n_features=len(features),
        hidden_size=hidden_size,
        num_layers=num_layers,
        dropout=dropout,
        val_split=val_split,
        batch_size=batch_size,
        epochs=epochs,
        lr=lr,
        weight_decay=wd,
        early_stop_patience=patience,
        device=args.device,
    )

    model_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), model_path)
    normalizer.save(normalizer_path)
    log.info("Saved model → %s", model_path)
    log.info("Saved normalizer → %s", normalizer_path)


if __name__ == "__main__":
    main()
