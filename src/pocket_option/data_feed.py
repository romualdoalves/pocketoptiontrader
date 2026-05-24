"""
Feed de dados em tempo real da PocketOption.
Delega ao connector (que mantém estado via eventos Socket.IO).
"""
import time
import logging
from typing import Optional

import pandas as pd

from .connector import PocketOptionConnector

logger = logging.getLogger(__name__)


class DataFeed:
    """
    Fornece preço atual, payout e candles históricos.
    Os dados chegam via eventos Socket.IO e ficam em cache no conector.
    """

    def __init__(self, connector: PocketOptionConnector) -> None:
        self._conn = connector

    # ── Preço atual ───────────────────────────────────────────────────────────

    def get_current_price(self, asset: str) -> Optional[float]:
        price = self._conn.get_realtime_price(asset)
        if price and price > 0:
            return price
        logger.debug("Preço de %s ainda não disponível no cache", asset)
        return None

    # ── Candles ───────────────────────────────────────────────────────────────

    def get_candles(self, asset: str, interval_seconds: int, count: int) -> Optional[pd.DataFrame]:
        """
        Retorna DataFrame OHLCV com os últimos `count` candles de `interval_seconds`.
        Colunas: open, high, low, close, volume, time
        """
        raw = self._conn.get_candles(asset, interval_seconds, count, time.time())
        if not raw:
            return None
        try:
            df = pd.DataFrame(raw)
            # Normalizar colunas se necessário
            rename_map = {
                "o": "open",  "h": "high",  "l": "low",
                "c": "close", "v": "volume", "t": "time",
            }
            df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns}, inplace=True)
            return df
        except Exception as exc:
            logger.error("Erro ao converter candles de %s: %s", asset, exc)
            return None

    def get_candle_open(self, asset: str) -> Optional[float]:
        """Retorna o preço de abertura do candle M1 atual."""
        df = self.get_candles(asset, 60, 2)
        if df is not None and not df.empty and "open" in df.columns:
            try:
                return float(df.iloc[-1]["open"])
            except (KeyError, IndexError):
                pass
        # Fallback: usa preço atual como aproximação
        return self.get_current_price(asset)

    # ── Payout ────────────────────────────────────────────────────────────────

    def get_payout(self, asset: str) -> float:
        """
        Retorna payout atual como decimal (ex: 0.85 para 85%).
        Retorna 0.0 em caso de ausência (bloqueia operação por segurança).
        """
        raw = self._conn.get_payout(asset)
        if not raw:
            return 0.0
        # Normalizar: 85.0 → 0.85, 0.85 → 0.85
        return raw / 100.0 if raw > 1.0 else raw
