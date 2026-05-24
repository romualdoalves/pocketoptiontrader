"""
Gerenciador de trades binários na PocketOption.

Sincronização de t3:
  Na PocketOption, expirations_mode=1 (1 minuto) sincroniza automaticamente
  com o próximo limite de minuto inteiro. Ordem colocada às 10:00:45 expira
  às 10:01:00, igual a uma ordem colocada às 10:00:10.
  Isso garante que Order1 (t1) e Order2 (t2) expirem no mesmo t3.
"""
import math
import time
import logging
from dataclasses import dataclass
from typing import Optional

from .connector import PocketOptionConnector

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    order_id: str
    direction: str
    stake: float
    profit: float          # positivo se win, negativo/zero se loss
    status: str            # "win" | "loss" | "draw" | "timeout"
    close_price: float = 0.0


class TradeManager:
    """
    Executa e monitora trades binários.
    Interface de alto nível sobre o PocketOptionConnector.
    """

    POLL_INTERVAL    = 0.5   # segundos entre verificações de resultado
    RESULT_EXTRA_TTL = 10.0  # segundos além de t3 para aguardar resultado

    def __init__(self, connector: PocketOptionConnector) -> None:
        self._conn = connector

    # ── Tempo ─────────────────────────────────────────────────────────────────

    @staticmethod
    def next_minute_boundary() -> float:
        """Próximo limite de minuto inteiro (t3)."""
        return math.ceil(time.time() / 60) * 60

    @staticmethod
    def seconds_until(target: float) -> float:
        return max(0.0, target - time.time())

    # ── Execução ──────────────────────────────────────────────────────────────

    def place_trade(
        self,
        asset: str,
        stake: float,
        direction: str,   # "call" | "put"
        expiry_timestamp: float,
    ) -> Optional[str]:
        """
        Coloca um trade binário com expiração de 1 minuto sincronizada com t3.
        Edge case C: bloqueia se restar < 15s até t3.
        Edge case A: retorna None se a plataforma rejeitar.
        """
        seconds_left = self.seconds_until(expiry_timestamp)
        if seconds_left < 15:
            logger.warning(
                "place_trade bloqueado: %.0fs até expiração (mínimo 15s)", seconds_left
            )
            return None

        try:
            accepted, order_id = self._conn.buy(
                amount=float(stake),
                active=asset,
                action=direction,
                expirations_mode=1,
            )
        except Exception as exc:
            logger.error("Erro em buy(): %s", exc)
            self._conn.reconnect()
            return None

        if not accepted:
            logger.warning("Ordem rejeitada: %s %s stake=%.2f", direction, asset, stake)
            return None

        logger.info(
            "Trade aberto: %s %s stake=%.2f id=%s (%.0fs até expiração)",
            direction.upper(), asset, stake, order_id, seconds_left,
        )
        return str(order_id)

    # ── Resultado ─────────────────────────────────────────────────────────────

    def wait_for_result(
        self,
        order_id: str,
        expiry_timestamp: float,
        stake: float = 0.0,
        direction: str = "",
    ) -> Optional[TradeResult]:
        """
        Faz polling até o resultado chegar (max expiry + RESULT_EXTRA_TTL).
        Retorna TradeResult com status "timeout" se não houver resultado.
        """
        deadline = expiry_timestamp + self.RESULT_EXTRA_TTL

        while time.time() < deadline:
            status_raw, profit = self._conn.check_win(order_id)

            if status_raw is not None:
                logger.info(
                    "Resultado %s: %s profit=%.4f", order_id, status_raw, profit
                )
                return TradeResult(
                    order_id=order_id,
                    direction=direction,
                    stake=stake,
                    profit=float(profit),
                    status=status_raw,
                )

            time.sleep(self.POLL_INTERVAL)

        logger.warning("Timeout aguardando resultado da ordem %s", order_id)
        return TradeResult(
            order_id=order_id,
            direction=direction,
            stake=stake,
            profit=0.0,
            status="timeout",
        )
