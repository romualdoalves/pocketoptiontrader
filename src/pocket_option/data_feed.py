"""
Feed de dados em tempo real da PocketOption.
Preço atual via WebSocket, candles históricos via REST.
"""
import time
import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .connector import PocketOptionConnector

logger = logging.getLogger(__name__)


@dataclass
class Tick:
    timestamp: float
    price: float
    asset: str


class DataFeed:
    """
    Fornece preço atual e candles históricos para qualquer par da PocketOption.
    Mantém um buffer em memória dos últimos ticks por ativo.
    """

    def __init__(self, connector: PocketOptionConnector, buffer_size: int = 500) -> None:
        self._conn = connector
        self._buffer_size = buffer_size
        self._ticks: dict[str, deque] = {}
        self._lock = threading.Lock()

    # ── Preço atual ───────────────────────────────────────────────────────────

    def get_current_price(self, asset: str) -> Optional[float]:
        """Retorna o último preço disponível do ativo."""
        try:
            raw = self._conn.api.get_realtime_price(asset)
            price = float(raw)
            self._record_tick(asset, price)
            return price
        except Exception as exc:
            logger.error("get_current_price(%s): %s", asset, exc)
            return None

    def _record_tick(self, asset: str, price: float) -> None:
        tick = Tick(timestamp=time.time(), price=price, asset=asset)
        with self._lock:
            if asset not in self._ticks:
                self._ticks[asset] = deque(maxlen=self._buffer_size)
            self._ticks[asset].append(tick)

    # ── Candles ───────────────────────────────────────────────────────────────

    def get_candles(self, asset: str, interval_seconds: int, count: int) -> Optional[pd.DataFrame]:
        """
        Retorna um DataFrame OHLCV com `count` candles de `interval_seconds`.
        Colunas: open, high, low, close, volume, time
        """
        try:
            raw = self._conn.api.get_candles(asset, interval_seconds, count, time.time())
            if raw is None or (isinstance(raw, pd.DataFrame) and raw.empty):
                return None
            if isinstance(raw, pd.DataFrame):
                return raw
            # Fallback: lista de dicts
            return pd.DataFrame(raw)
        except Exception as exc:
            logger.error("get_candles(%s, %ds, %d): %s", asset, interval_seconds, count, exc)
            return None

    def get_candle_open(self, asset: str) -> Optional[float]:
        """Retorna o preço de abertura do candle M1 atual."""
        df = self.get_candles(asset, 60, 2)
        if df is not None and not df.empty:
            try:
                return float(df.iloc[-1]["open"])
            except (KeyError, IndexError):
                pass
        return self.get_current_price(asset)

    # ── Payout ────────────────────────────────────────────────────────────────

    def get_payout(self, asset: str) -> float:
        """
        Retorna o payout atual como decimal (ex: 0.85 para 85%).
        Retorna 0.0 em caso de falha (rejeita a operação por segurança).
        """
        try:
            raw = self._conn.api.get_payout(asset)
            if raw is None:
                return 0.0
            payout = float(raw)
            return payout / 100.0 if payout > 1.0 else payout
        except Exception as exc:
            logger.warning("get_payout(%s): %s", asset, exc)
            return 0.0
