"""
PocketOption WebSocket connector.
Autenticação via cookie ci_session (POCKET_SSID) extraído do navegador.
Reconexão automática com backoff exponencial.
"""
import os
import time
import logging
from typing import Optional

from pocketoptionapi.stable_api import PocketOption as _POApi

logger = logging.getLogger(__name__)


class PocketOptionConnector:
    """Gerencia o ciclo de vida da conexão WebSocket com a PocketOption."""

    MAX_RECONNECT_DELAY = 60  # segundos

    def __init__(self) -> None:
        self._ssid: str = os.environ["POCKET_SSID"]
        self._demo: bool = bool(int(os.environ.get("POCKET_DEMO", "1")))
        self._api: Optional[_POApi] = None
        self._reconnect_delay: int = 1

    # ── Conexão ───────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Estabelece conexão WebSocket. Bloqueia até conectar com sucesso."""
        attempt = 0
        while True:
            attempt += 1
            try:
                logger.info("Conectando à PocketOption (tentativa %d, demo=%s)", attempt, self._demo)
                self._api = _POApi(ssid=self._ssid, demo=self._demo)
                self._api.connect()
                self._reconnect_delay = 1
                logger.info("Conexão estabelecida com sucesso")
                return
            except Exception as exc:
                logger.error("Falha na conexão: %s. Tentando novamente em %ds", exc, self._reconnect_delay)
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self.MAX_RECONNECT_DELAY)

    def disconnect(self) -> None:
        if self._api is not None:
            try:
                self._api.disconnect()
            except Exception:
                pass
            self._api = None
            logger.info("Desconectado da PocketOption")

    def reconnect(self) -> None:
        """Reconecta após perda de conexão (edge case D do PRD)."""
        logger.warning("Reconectando...")
        self.disconnect()
        self.connect()

    # ── Acesso à API ─────────────────────────────────────────────────────────

    @property
    def api(self) -> _POApi:
        """Retorna a instância da API, conectando se necessário."""
        if self._api is None:
            self.connect()
        return self._api  # type: ignore[return-value]

    @property
    def is_connected(self) -> bool:
        return self._api is not None

    @property
    def is_demo(self) -> bool:
        return self._demo

    # ── Atalhos de conta ─────────────────────────────────────────────────────

    def get_balance(self) -> float:
        return float(self.api.get_balance())
