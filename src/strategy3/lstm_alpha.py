"""
lstm_alpha.py — LSTM alpha model for Strategy 3.

Predicts next-N-bar log return from a window of engineered features.
Inference is gated through a soft threshold to emit {+1, 0, -1} directional
signals which the allocator uses as an alpha overlay.

Torch is a heavy optional dependency — module degrades gracefully if torch
is missing so the rest of Strategy 3 can still run (signal = 0 for all).
"""

from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

try:
    import torch
    import torch.nn as nn
    _TORCH_OK = True
except Exception as e:  # pragma: no cover
    log.warning("PyTorch not available (%s) — LSTM alpha will emit neutral signals.", e)
    torch = None  # type: ignore
    nn = None     # type: ignore
    _TORCH_OK = False


# ---------------------------------------------------------------------------
# Model definition
# ---------------------------------------------------------------------------

if _TORCH_OK:

    class LSTMModel(nn.Module):
        """
        Stacked LSTM → Linear head predicting a single scalar
        (target: log return N bars ahead).
        """

        def __init__(
            self,
            n_features: int,
            hidden_size: int = 64,
            num_layers: int = 2,
            dropout: float = 0.2,
        ):
            super().__init__()
            self.lstm = nn.LSTM(
                input_size=n_features,
                hidden_size=hidden_size,
                num_layers=num_layers,
                dropout=dropout if num_layers > 1 else 0.0,
                batch_first=True,
            )
            self.head = nn.Sequential(
                nn.Linear(hidden_size, hidden_size // 2),
                nn.ReLU(),
                nn.Dropout(dropout),
                nn.Linear(hidden_size // 2, 1),
            )

        def forward(self, x: "torch.Tensor") -> "torch.Tensor":
            out, _ = self.lstm(x)
            last = out[:, -1, :]
            return self.head(last).squeeze(-1)


# ---------------------------------------------------------------------------
# Normalizer (per-feature mean/std stored as JSON alongside the weights)
# ---------------------------------------------------------------------------

class Normalizer:
    def __init__(self, means: Dict[str, float], stds: Dict[str, float]):
        self.means = means
        self.stds  = stds

    @classmethod
    def fit(cls, df: pd.DataFrame, features: List[str]) -> "Normalizer":
        means = {f: float(df[f].mean()) for f in features}
        stds  = {f: float(df[f].std() or 1.0) for f in features}
        return cls(means, stds)

    def transform(self, df: pd.DataFrame, features: List[str]) -> np.ndarray:
        cols = []
        for f in features:
            m = self.means.get(f, 0.0)
            s = self.stds.get(f, 1.0) or 1.0
            cols.append(((df[f].values - m) / s).astype(np.float32))
        return np.stack(cols, axis=-1)  # shape: (T, F)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"means": self.means, "stds": self.stds}, f, indent=2)

    @classmethod
    def load(cls, path: Path) -> "Normalizer":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(data["means"], data["stds"])


# ---------------------------------------------------------------------------
# Preprocess
# ---------------------------------------------------------------------------

def preprocess(
    df: pd.DataFrame,
    features: List[str],
    sequence_length: int,
    normalizer: Normalizer,
) -> Optional[np.ndarray]:
    """
    Build a single (1, seq_len, n_features) tensor from the last `sequence_length`
    rows of `df`. Returns None if not enough rows or any required feature is missing.
    """
    if df is None or len(df) < sequence_length:
        return None
    missing = [f for f in features if f not in df.columns]
    if missing:
        log.warning("preprocess: missing features %s", missing)
        return None

    window = df.tail(sequence_length)
    arr = normalizer.transform(window, features)        # (T, F)
    if np.isnan(arr).any() or np.isinf(arr).any():
        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    return arr[np.newaxis, :, :]                        # (1, T, F)


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

def predict_return(
    model,
    df: pd.DataFrame,
    features: List[str],
    sequence_length: int,
    normalizer: Normalizer,
    device: str = "cpu",
) -> Optional[float]:
    """Return predicted log return (float) or None if inputs are unusable."""
    if not _TORCH_OK or model is None:
        return None
    x = preprocess(df, features, sequence_length, normalizer)
    if x is None:
        return None
    model.eval()
    with torch.no_grad():
        t = torch.from_numpy(x).to(device)
        y = model(t).cpu().numpy().ravel()[0]
    return float(y)


def predict_signal(
    model,
    df: pd.DataFrame,
    features: List[str],
    sequence_length: int,
    normalizer: Normalizer,
    long_threshold: float,
    short_threshold: float,
    device: str = "cpu",
) -> Dict[str, float]:
    """
    Discrete directional signal from the alpha model.
    Returns {"signal": -1|0|+1, "pred": float, "confidence": float}.
    """
    pred = predict_return(model, df, features, sequence_length, normalizer, device)
    if pred is None:
        return {"signal": 0, "pred": 0.0, "confidence": 0.0}

    if pred >= long_threshold:
        sig = 1
    elif pred <= short_threshold:
        sig = -1
    else:
        sig = 0

    # Rough confidence: how far past the threshold we are (capped at 1.0)
    denom = max(abs(long_threshold), abs(short_threshold), 1e-9)
    conf  = float(min(abs(pred) / denom, 3.0) / 3.0)

    return {"signal": sig, "pred": float(pred), "confidence": conf}


# ---------------------------------------------------------------------------
# Load / init helpers
# ---------------------------------------------------------------------------

def load_or_init(
    model_path: str,
    normalizer_path: str,
    n_features: int,
    hidden_size: int = 64,
    num_layers: int = 2,
    dropout: float = 0.2,
    device: str = "cpu",
) -> Tuple[Optional["LSTMModel"], Optional[Normalizer]]:
    """
    Try to load a trained LSTM + normalizer from disk. Return (None, None) if
    torch is unavailable or files are missing — caller should treat that as
    "alpha model offline, use macro + allocator only".
    """
    if not _TORCH_OK:
        return None, None

    mp = Path(model_path)
    np_ = Path(normalizer_path)

    if not mp.exists() or not np_.exists():
        log.warning("LSTM artifacts not found (%s / %s) — model offline.", mp, np_)
        return None, None

    try:
        normalizer = Normalizer.load(np_)
        model = LSTMModel(n_features, hidden_size, num_layers, dropout).to(device)
        state = torch.load(mp, map_location=device)
        model.load_state_dict(state)
        model.eval()
        log.info("LSTM loaded from %s (features=%d, hidden=%d, layers=%d)",
                 mp, n_features, hidden_size, num_layers)
        return model, normalizer
    except Exception as e:
        log.error("Failed to load LSTM (%s) — running without alpha model.", e)
        return None, None
