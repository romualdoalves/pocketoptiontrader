"""
Gerenciador de trades binários na PocketOption.

Conceito central de sincronização de expiração:
  Na PocketOption, ao selecionar expiração de 1 minuto, o trade expira no
  próximo limite de minuto inteiro (ex: 10:00:45 → expira às 10:01:00).
  Isso garante que Order1 (t1) e Order2 (t2) expirem exatamente em t3,
  mesmo que t1 ≠ t2.
"""
import math
import time
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from .connector import PocketOptionConnector

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    order_id: str
    direction: str
    stake: float
    profit: float          # positivo se win, negativo se loss
    status: str            # "win" | "loss" | "draw" | "timeout"
    close_price: float = 0.0


class TradeManager:
    """
    Executa e monitora trades binários.
    Todos os trades usam expiração de 1 minuto sincronizada com o
    próximo limite de minuto para garantir t3 igual para Order1 e Order2.
    """

    POLL_INTERVAL = 0.5     # segundos entre verificações de resultado
    RESULT_EXTRA_TTL = 8.0  # segundos após t3 para aguardar resultado

    def __init__(self, connector: PocketOptionConnector) -> None:
        self._conn = connector

    # ── Sincronização de tempo ────────────────────────────────────────────────

    @staticmethod
    def next_minute_boundary() -> float:
        """Retorna o Unix timestamp do próximo limite de minuto inteiro (t3)."""
        return math.ceil(time.time() / 60) * 60

    @staticmethod
    def seconds_until(target: float) -> float:
        return max(0.0, target - time.time())

    # ── Execução de trade ─────────────────────────────────────────────────────

    def place_trade(
        self,
        asset: str,
        stake: float,
        direction: str,  # "call" ou "put"
        expiry_timestamp: float,
    ) -> Optional[str]:
        """
        Coloca um trade binário expirando em expiry_timestamp.

        A PocketOption sincroniza expirations_mode=1 com o próximo limite
        de minuto — ambas as ordens no mesmo minuto expiram em t3 idêntico.

        Edge case C: retorna None se restar < 15s para t3.
        Edge case A: retorna None se a ordem for rejeitada pela plataforma.
        """
        seconds_left = self.seconds_until(expiry_timestamp)
        if seconds_left < 15:
            logger.warning(
                "place_trade bloqueado: apenas %.0fs até a expiração (mínimo: 15s)", seconds_left
            )
            return None

        try:
            check, order_id = self._conn.api.buy(
                amount=float(stake),
                active=asset,
                action=direction,
                expirations_mode=1,  # 1 minuto — plataforma sincroniza com t3
            )
        except Exception as exc:
            logger.error("buy() lançou exceção: %s", exc)
            self._conn.reconnect()
            return None

        if not check:
            logger.warning("Ordem rejeitada pela plataforma: %s %s stake=%.2f", direction, asset, stake)
            return None

        logger.info("Trade aberto: %s %s stake=%.2f id=%s (%.0fs até expiração)",
                    direction.upper(), asset, stake, order_id, seconds_left)
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
        Aguarda o resultado do trade por polling até expiry + RESULT_EXTRA_TTL.
        Retorna None em caso de timeout.
        """
        deadline = expiry_timestamp + self.RESULT_EXTRA_TTL

        while time.time() < deadline:
            try:
                status_raw, profit = self._conn.api.check_win(order_id)
                # pocketoptionapi retorna "win", "loose" (typo na lib) ou None/"processing"
                if status_raw in ("win", "loose", "loss"):
                    status = "win" if status_raw == "win" else "loss"
                    logger.info("Resultado trade %s: %s profit=%.4f", order_id, status, profit)
                    return TradeResult(
                        order_id=order_id,
                        direction=direction,
                        stake=stake,
                        profit=float(profit) if profit is not None else 0.0,
                        status=status,
                    )
            except Exception as exc:
                logger.debug("check_win(%s): %s", order_id, exc)

            time.sleep(self.POLL_INTERVAL)

        logger.warning("Timeout aguardando resultado do trade %s", order_id)
        return TradeResult(
            order_id=order_id,
            direction=direction,
            stake=stake,
            profit=0.0,
            status="timeout",
        )
