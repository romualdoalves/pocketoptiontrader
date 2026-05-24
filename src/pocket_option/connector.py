"""
PocketOption WebSocket Connector — implementação direta com python-socketio.

A PocketOption usa o protocolo Socket.IO (Engine.IO 4) para comunicação
em tempo real. Autenticação via cookie ci_session (POCKET_SSID).

Importante: este conector loga TODOS os eventos recebidos no nível DEBUG.
Para inspecionar o protocolo real, rode com LOG_LEVEL=DEBUG.
"""
import os
import json
import time
import uuid
import threading
import logging
from typing import Optional, Callable

import socketio

logger = logging.getLogger(__name__)


# ── Constantes do protocolo ───────────────────────────────────────────────────

# URLs conhecidas do WebSocket PocketOption (tenta em sequência)
_WS_URLS = [
    "wss://api.po.market",
    "wss://trading.po.market",
    "wss://ws.pocketoption.com",
]

# Modo demo: conta demo = 1, conta real = 0
_DEMO_VALUE = {True: 1, False: 0}


# ── Conector principal ────────────────────────────────────────────────────────

class PocketOptionConnector:
    """
    Gerencia o ciclo de vida da conexão Socket.IO com a PocketOption.

    Fornece interface de alto nível idêntica à usada por DataFeed e TradeManager:
      get_balance()
      get_realtime_price(asset)
      get_candles(asset, interval, count, end_time)
      get_payout(asset)
      buy(amount, active, action, expirations_mode) → (bool, order_id)
      check_win(order_id)                           → (status, profit)
    """

    MAX_RECONNECT_DELAY = 60
    CONNECT_TIMEOUT     = 30  # segundos para aguardar conexão inicial

    def __init__(self) -> None:
        self._ssid    = os.environ["POCKET_SSID"]
        self._uid     = os.environ.get("POCKET_UID", "")
        self._secret  = os.environ.get("POCKET_SECRET", "")
        self._demo    = bool(int(os.environ.get("POCKET_DEMO", "1")))

        self._sio: Optional[socketio.Client] = None
        self._connected    = False
        self._connect_evt  = threading.Event()
        self._lock         = threading.Lock()
        self._reconnect_delay = 1

        # Estado em memória
        self._balance: float             = 0.0
        self._prices: dict[str, float]   = {}
        self._payouts: dict[str, float]  = {}
        self._candles: dict[str, list]   = {}
        self._candle_evt = threading.Event()

        # Resultados de ordens: order_id → {"status": str, "profit": float}
        self._order_results: dict[str, dict] = {}
        self._order_events: dict[str, threading.Event] = {}

    # ── Conexão ───────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Conecta ao WebSocket PocketOption. Bloqueia até conectar."""
        self._connect_evt.clear()
        self._build_sio()

        cookies = f"ci_session={self._ssid}"
        headers = {
            "Cookie": cookies,
            "Origin": "https://pocketoption.com",
        }

        connected = False
        for url in _WS_URLS:
            try:
                logger.info("Tentando conectar em %s (demo=%s)", url, self._demo)
                self._sio.connect(
                    url,
                    headers=headers,
                    transports=["websocket"],
                    wait_timeout=self.CONNECT_TIMEOUT,
                )
                connected = True
                break
            except Exception as exc:
                logger.warning("Falha em %s: %s", url, exc)

        if not connected:
            raise ConnectionError("Não foi possível conectar à PocketOption via WebSocket")

        if not self._connect_evt.wait(timeout=self.CONNECT_TIMEOUT):
            raise ConnectionError("Timeout aguardando confirmação de conexão")

        logger.info("PocketOption conectada com sucesso (demo=%s, uid=%s)", self._demo, self._uid)

    def disconnect(self) -> None:
        if self._sio and self._connected:
            try:
                self._sio.disconnect()
            except Exception:
                pass
        self._sio = None
        self._connected = False

    def reconnect(self) -> None:
        """Edge case D: reconecta após perda de conexão."""
        logger.warning("Reconectando à PocketOption...")
        self.disconnect()
        time.sleep(self._reconnect_delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, self.MAX_RECONNECT_DELAY)
        self.connect()
        self._reconnect_delay = 1

    # ── Interface da API (chamada por DataFeed e TradeManager) ────────────────

    def get_balance(self) -> float:
        return self._balance

    def get_realtime_price(self, asset: str) -> float:
        return self._prices.get(asset, 0.0)

    def get_payout(self, asset: str) -> float:
        return self._payouts.get(asset, 0.0)

    def get_candles(
        self, asset: str, interval: int, count: int, end_time: float
    ) -> Optional[list]:
        """
        Solicita candles históricos.
        Retorna lista de dicts {open, high, low, close, volume, time} ou None.
        """
        key = f"{asset}_{interval}"
        self._candle_evt.clear()
        self._sio.emit("getCandles", {
            "asset":    asset,
            "period":   interval,
            "count":    count,
            "time":     int(end_time),
            "isDemo":   _DEMO_VALUE[self._demo],
        })
        self._candle_evt.wait(timeout=5)
        return self._candles.get(key)

    def buy(
        self, amount: float, active: str, action: str, expirations_mode: int
    ) -> tuple[bool, str]:
        """
        Coloca uma ordem binária.
        action: "call" | "put"
        expirations_mode: 1 = 1 minuto (plataforma sincroniza com t3)
        Retorna (sucesso, order_id)
        """
        order_id = str(uuid.uuid4())
        evt = threading.Event()

        with self._lock:
            self._order_events[order_id] = evt

        payload = {
            "requestId": order_id,
            "asset":     active,
            "amount":    float(amount),
            "action":    action,       # "call" ou "put"
            "time":      expirations_mode * 60,  # segundos
            "isDemo":    _DEMO_VALUE[self._demo],
        }
        logger.debug("Enviando openOrder: %s", payload)
        self._sio.emit("openOrder", payload)

        # Aguarda confirmação de abertura (max 5s)
        accepted = evt.wait(timeout=5)
        if not accepted:
            logger.warning("Timeout aguardando confirmação da ordem %s", order_id)
            with self._lock:
                self._order_events.pop(order_id, None)
            return False, order_id

        return True, order_id

    def check_win(self, order_id: str) -> tuple[Optional[str], float]:
        """
        Verifica o resultado de uma ordem.
        Retorna ("win"|"loss"|"draw"|None, profit)
        None significa que o resultado ainda não chegou.
        """
        with self._lock:
            result = self._order_results.get(order_id)
        if result:
            return result["status"], result["profit"]
        return None, 0.0

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_demo(self) -> bool:
        return self._demo

    # ── Construção do Socket.IO client ────────────────────────────────────────

    def _build_sio(self) -> None:
        self._sio = socketio.Client(
            reconnection=False,      # gerenciamos reconexão manualmente
            logger=False,
            engineio_logger=False,
        )

        # ── Eventos de ciclo de vida ──────────────────────────────────────────

        @self._sio.event
        def connect():
            self._connected = True
            self._connect_evt.set()
            # Enviar autenticação após conexão
            self._sio.emit("auth", {
                "session": self._ssid,
                "uid":     self._uid,
                "isDemo":  _DEMO_VALUE[self._demo],
            })
            logger.debug("Handshake de autenticação enviado")

        @self._sio.event
        def disconnect():
            self._connected = False
            logger.info("WebSocket desconectado")

        @self._sio.event
        def connect_error(data):
            logger.error("Erro de conexão Socket.IO: %s", data)
            self._connect_evt.set()  # desbloqueia para não travar

        # ── Eventos de dados ──────────────────────────────────────────────────

        # Saldo da conta
        @self._sio.on("balance")
        @self._sio.on("updateBalance")
        def on_balance(data):
            try:
                bal = data.get("balance") or data.get("amount") or data
                self._balance = float(bal)
                logger.debug("Saldo atualizado: %.2f", self._balance)
            except (TypeError, ValueError):
                pass

        # Preço em tempo real (candles/ticks do ativo)
        @self._sio.on("updateStream")
        @self._sio.on("tick")
        @self._sio.on("quotes")
        def on_price(data):
            try:
                asset = data.get("asset") or data.get("symbol") or data.get("active")
                price = data.get("price") or data.get("close") or data.get("value")
                if asset and price:
                    self._prices[str(asset)] = float(price)
            except (TypeError, KeyError):
                pass

        # Payout
        @self._sio.on("payout")
        @self._sio.on("payouts")
        def on_payout(data):
            try:
                if isinstance(data, list):
                    for item in data:
                        asset   = item.get("asset") or item.get("symbol")
                        payout  = item.get("payout") or item.get("value")
                        if asset and payout:
                            self._payouts[str(asset)] = float(payout)
                elif isinstance(data, dict):
                    asset   = data.get("asset") or data.get("symbol")
                    payout  = data.get("payout") or data.get("value")
                    if asset and payout:
                        self._payouts[str(asset)] = float(payout)
            except (TypeError, KeyError):
                pass

        # Candles históricos
        @self._sio.on("candles")
        @self._sio.on("history")
        def on_candles(data):
            try:
                asset    = data.get("asset") or data.get("symbol")
                interval = data.get("period") or data.get("interval", 60)
                candles  = data.get("candles") or data.get("data") or []
                if asset:
                    key = f"{asset}_{interval}"
                    self._candles[key] = candles
                    self._candle_evt.set()
            except (TypeError, KeyError):
                pass

        # Confirmação de abertura de ordem
        @self._sio.on("successopenOrder")
        @self._sio.on("orderAccepted")
        @self._sio.on("openOrder")
        def on_order_opened(data):
            req_id = (data.get("requestId") or data.get("id") or "")
            logger.info("Ordem confirmada: %s", data)
            with self._lock:
                evt = self._order_events.get(str(req_id))
                if evt:
                    evt.set()

        # Resultado de ordem (win/loss)
        @self._sio.on("closeOrder")
        @self._sio.on("orderResult")
        @self._sio.on("tradeResult")
        def on_order_result(data):
            try:
                req_id = str(data.get("requestId") or data.get("id") or "")
                raw_status = (data.get("result") or data.get("status") or "").lower()
                profit = float(data.get("profit") or data.get("amount") or 0)

                # Normalizar status
                if raw_status in ("win", "won", "success"):
                    status = "win"
                elif raw_status in ("loose", "loss", "lost", "fail"):
                    status = "loss"
                elif raw_status in ("draw", "tie"):
                    status = "draw"
                else:
                    status = "loss"

                with self._lock:
                    self._order_results[req_id] = {"status": status, "profit": profit}
                logger.info("Resultado da ordem %s: %s profit=%.4f", req_id, status, profit)
            except (TypeError, ValueError, KeyError) as exc:
                logger.debug("on_order_result parse error: %s — data=%s", exc, data)

        # ── Catch-all: loga todos os eventos não tratados (ajuda protocolo) ──
        @self._sio.on("*")
        def on_any(event, data):
            logger.debug("[WebSocket raw] event=%s data=%s", event,
                         str(data)[:200] if data else "")
