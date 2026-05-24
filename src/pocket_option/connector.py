"""
PocketOption WebSocket Connector — websocket-client com protocolo Engine.IO/Socket.IO manual.

PocketOption usa Socket.IO sobre Engine.IO v4.  python-socketio recusava a
conexão porque a URL precisa ser passada com path e query completos:
  wss://api.po.market/socket.io/?EIO=4&transport=websocket

Além disso, o servidor exige headers de browser (User-Agent, Origin).
Nesta versão usamos websocket-client para controle total do handshake.
"""
import os
import ssl
import json
import time
import uuid
import threading
import logging
from typing import Optional

import websocket

logger = logging.getLogger(__name__)

# ── URLs — path e query obrigatórios ─────────────────────────────────────────

_WS_URLS = [
    "wss://api.po.market/socket.io/?EIO=4&transport=websocket",
    "wss://trading.po.market/socket.io/?EIO=4&transport=websocket",
    "wss://ws.pocketoption.com/socket.io/?EIO=4&transport=websocket",
]

_BROWSER_HEADERS = [
    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Origin: https://pocketoption.com",
    "Referer: https://pocketoption.com/",
    "Accept-Language: en-US,en;q=0.9",
]

_DEMO_VALUE = {True: 1, False: 0}


# ── Conector ──────────────────────────────────────────────────────────────────

class PocketOptionConnector:
    """
    Gerencia o ciclo de vida da conexão WebSocket com a PocketOption.

    Interface pública (idêntica à versão anterior):
      get_balance() → float
      get_realtime_price(asset) → float
      get_payout(asset) → float
      get_candles(asset, interval, count, end_time) → list | None
      buy(amount, active, action, expirations_mode) → (bool, str)
      check_win(order_id) → (str|None, float)
      is_connected → bool  (property)
    """

    MAX_RECONNECT_DELAY = 60
    CONNECT_TIMEOUT     = 30

    def __init__(self) -> None:
        self._ssid    = os.environ.get("POCKET_SSID", "")          # cookie ci_session (header HTTP)
        self._secret  = os.environ["POCKET_SECRET"]                 # sessionToken para auth WS
        self._uid     = os.environ.get("POCKET_UID", "")
        self._demo    = bool(int(os.environ.get("POCKET_DEMO", "1")))

        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._connected    = False
        self._connect_evt  = threading.Event()
        self._lock         = threading.Lock()
        self._reconnect_delay = 1

        # Estado em memória
        self._balance: float            = 0.0
        self._prices:  dict[str, float] = {}
        self._payouts: dict[str, float] = {}
        self._candles: dict[str, list]  = {}
        self._candle_evt = threading.Event()

        self._order_results: dict[str, dict]            = {}
        self._order_events:  dict[str, threading.Event] = {}

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    def connect(self) -> None:
        self._connect_evt.clear()
        self._connected = False

        headers = _BROWSER_HEADERS + [f"Cookie: ci_session={self._ssid}"]

        connected = False
        for url in _WS_URLS:
            try:
                logger.info("Tentando conectar em %s (demo=%s)", url, self._demo)
                self._ws = websocket.WebSocketApp(
                    url,
                    header=headers,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws_thread = threading.Thread(
                    target=self._ws.run_forever,
                    kwargs={"sslopt": {"cert_reqs": ssl.CERT_NONE},
                            "ping_interval": 20,
                            "ping_timeout": 10},
                    daemon=True,
                    name="ws-pocketoption",
                )
                self._ws_thread.start()

                if self._connect_evt.wait(timeout=self.CONNECT_TIMEOUT):
                    if self._connected:
                        connected = True
                        break
                    logger.warning("Auth falhou em %s — tentando próximo", url)
                else:
                    logger.warning("Timeout em %s — tentando próximo", url)

                self._ws.close()
                if self._ws_thread.is_alive():
                    self._ws_thread.join(timeout=3)

            except Exception as exc:
                logger.warning("Falha em %s: %s", url, exc)

        if not connected:
            raise ConnectionError("Não foi possível conectar à PocketOption via WebSocket")

        logger.info("PocketOption conectada (demo=%s, uid=%s)", self._demo, self._uid)

    def disconnect(self) -> None:
        self._connected = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None

    def reconnect(self) -> None:
        logger.warning("Reconectando à PocketOption...")
        self.disconnect()
        time.sleep(self._reconnect_delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, self.MAX_RECONNECT_DELAY)
        self.connect()
        self._reconnect_delay = 1

    # ── Interface de dados ────────────────────────────────────────────────────

    def get_balance(self) -> float:
        return self._balance

    def get_realtime_price(self, asset: str) -> float:
        return self._prices.get(asset, 0.0)

    def get_payout(self, asset: str) -> float:
        return self._payouts.get(asset, 0.0)

    def get_candles(
        self, asset: str, interval: int, count: int, end_time: float
    ) -> Optional[list]:
        key = f"{asset}_{interval}"
        self._candle_evt.clear()
        self._emit("getCandles", {
            "asset":  asset,
            "period": interval,
            "count":  count,
            "time":   int(end_time),
            "isDemo": _DEMO_VALUE[self._demo],
        })
        self._candle_evt.wait(timeout=5)
        return self._candles.get(key)

    def buy(
        self, amount: float, active: str, action: str, expirations_mode: int
    ) -> tuple[bool, str]:
        order_id = str(uuid.uuid4())
        evt = threading.Event()

        with self._lock:
            self._order_events[order_id] = evt

        self._emit("openOrder", {
            "requestId": order_id,
            "asset":     active,
            "amount":    float(amount),
            "action":    action,
            "time":      expirations_mode * 60,
            "isDemo":    _DEMO_VALUE[self._demo],
        })

        accepted = evt.wait(timeout=5)
        if not accepted:
            logger.warning("Timeout aguardando confirmação da ordem %s", order_id)
            with self._lock:
                self._order_events.pop(order_id, None)
            return False, order_id

        return True, order_id

    def check_win(self, order_id: str) -> tuple[Optional[str], float]:
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

    # ── Envio de mensagem Socket.IO ───────────────────────────────────────────

    def _emit(self, event: str, data: dict) -> None:
        if self._ws and self._connected:
            msg = f"42{json.dumps([event, data])}"
            logger.debug("Emitindo: %s", msg[:200])
            self._ws.send(msg)

    # ── Handlers WebSocket ────────────────────────────────────────────────────

    def _on_open(self, ws) -> None:
        logger.debug("TCP/TLS aberto — enviando Socket.IO CONNECT (40)")
        ws.send("40")

    def _start_ping_loop(self, ws) -> None:
        """Envia 42["ping-server"] periodicamente, como o browser faz."""
        def _loop():
            while self._connected:
                time.sleep(25)
                if self._connected:
                    try:
                        ws.send('42["ping-server"]')
                    except Exception:
                        break
        t = threading.Thread(target=_loop, daemon=True, name="ws-ping")
        t.start()

    def _on_message(self, ws, message: str) -> None:
        logger.debug("[WS raw] %s", message[:300])

        try:
            if message.startswith("0"):
                # Engine.IO OPEN — servidor confirma sessão
                logger.debug("Engine.IO OPEN recebido")

            elif message == "2":
                # Engine.IO PING — responder com PONG
                ws.send("3")

            elif message.startswith("40"):
                # Socket.IO CONNECT confirmado — autenticar
                logger.info("Socket.IO conectado — enviando auth")
                url_path = ("cabinet/demo-quick-high-low"
                            if self._demo else "cabinet/quick-high-low")
                auth = json.dumps(["auth", {
                    "sessionToken": self._secret,
                    "uid":          self._uid,
                    "lang":         "en",
                    "currentUrl":   url_path,
                    "isChart":      1,
                }])
                ws.send(f"42{auth}")

            elif message.startswith("42"):
                # Socket.IO EVENT
                payload = json.loads(message[2:])
                if isinstance(payload, list) and payload:
                    event = payload[0]
                    data  = payload[1] if len(payload) > 1 else {}
                    self._handle_event(event, data)

        except Exception as exc:
            logger.debug("Erro ao processar mensagem: %s — raw=%s", exc, message[:100])

    def _on_error(self, ws, error) -> None:
        logger.error("Erro WebSocket: %s", error)
        self._connect_evt.set()  # desbloqueia connect() em caso de erro

    def _on_close(self, ws, code, msg) -> None:
        self._connected = False
        logger.info("WebSocket fechado (code=%s): %s", code, msg)

    # ── Despacho de eventos ───────────────────────────────────────────────────

    def _handle_event(self, event: str, data) -> None:
        logger.debug("[event] %s → %s", event, str(data)[:200])

        # Autenticação confirmada — evento real do servidor
        if event in ("auth/success", "successAuth", "successauth", "authenticated"):
            self._connected = True
            self._connect_evt.set()
            self._start_ping_loop(self._ws)
            logger.info("Autenticado com sucesso (evento: %s)", event)

        # Saldo — também serve como confirmação de auth bem-sucedida
        elif event in ("balance", "updateBalance"):
            try:
                bal = (data.get("balance") or data.get("amount")
                       if isinstance(data, dict) else data)
                self._balance = float(bal)
                logger.debug("Saldo: %.2f", self._balance)
                if not self._connected:
                    self._connected = True
                    self._connect_evt.set()
            except (TypeError, ValueError):
                pass

        # Preço em tempo real
        elif event in ("updateStream", "tick", "quotes"):
            try:
                asset = (data.get("asset") or data.get("symbol")
                         or data.get("active"))
                price = (data.get("price") or data.get("close")
                         or data.get("value"))
                if asset and price:
                    self._prices[str(asset)] = float(price)
            except (TypeError, KeyError):
                pass

        # Payout
        elif event in ("payout", "payouts"):
            try:
                items = data if isinstance(data, list) else [data]
                for item in items:
                    asset  = item.get("asset") or item.get("symbol")
                    payout = item.get("payout") or item.get("value")
                    if asset and payout:
                        self._payouts[str(asset)] = float(payout)
            except (TypeError, KeyError):
                pass

        # Candles históricos
        elif event in ("candles", "history"):
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
        elif event in ("successopenOrder", "orderAccepted", "openOrder"):
            req_id = str(data.get("requestId") or data.get("id") or "")
            logger.info("Ordem confirmada: %s", data)
            with self._lock:
                evt = self._order_events.get(req_id)
                if evt:
                    evt.set()

        # Resultado de ordem
        elif event in ("closeOrder", "orderResult", "tradeResult"):
            try:
                req_id     = str(data.get("requestId") or data.get("id") or "")
                raw_status = (data.get("result") or data.get("status") or "").lower()
                profit     = float(data.get("profit") or data.get("amount") or 0)

                if raw_status in ("win", "won", "success"):
                    status = "win"
                elif raw_status in ("loose", "loss", "lost", "fail"):
                    status = "loss"
                else:
                    status = "draw"

                with self._lock:
                    self._order_results[req_id] = {"status": status, "profit": profit}
                logger.info("Resultado %s: %s profit=%.4f", req_id, status, profit)
            except (TypeError, ValueError, KeyError) as exc:
                logger.debug("on_order_result parse error: %s", exc)
