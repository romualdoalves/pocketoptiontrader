"""
Bot Farejador de Faixa — Range Sniper Strategy (PocketOption)

Coloca dois trades opostos (CALL + PUT) dentro da mesma janela de expiração
de 1 minuto, criando uma "zona de ganho duplo" entre os dois preços de entrada.

Matemática (payout 85%, stake $1 por ordem, risco total $2):
  Ambos ganham: preço entre P1 e P2  → +$0.85 × 2 = +$1.70
  Um ganha:     preço > P2 ou < P1   → +$0.85 − $1 = −$0.15
  Ambos perdem: IMPOSSÍVEL           → P1 < P2, logo não existe preço
                                        simultaneamente < P1 e > P2.

Break-even: P(ambos ganham) > 0.15 / 1.85 = 8.1%

Fluxo do ciclo (janela de 1 minuto):
  t0  → alinha no início do novo candle M1
  t1  → aguarda entry_wait_seconds (10s), observa direção do candle
  t1+ → coloca Ordem 1 (CALL se preço subiu, PUT se caiu)
  t1..t3-15s → monitora preço; quando atinge pip_distance, coloca Ordem 2
  t3  → ambas as ordens expiram (mesmo timestamp de minuto)
"""
import math
import time
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.pocket_option.trade_manager import TradeManager, TradeResult
from src.pocket_option.data_feed import DataFeed

logger = logging.getLogger(__name__)


# ── Configuração ──────────────────────────────────────────────────────────────

@dataclass
class RangeSniperConfig:
    asset: str = "EURUSD_otc"          # Par OTC para operação 24/7
    stake: float = 1.0                 # Valor por ordem em USD
    min_payout: float = 0.85           # Payout mínimo (85%)
    pip_distance: float = 0.0003       # Distância para trigger de Ordem 2 (3 pips)
    entry_wait_seconds: int = 10       # Aguardar após abertura do candle
    min_seconds_to_expiry: int = 15    # Edge case C: mín. segundos restantes
    price_poll_interval: float = 0.3   # Frequência de leitura de preço (s)


# ── Registro de ciclo ─────────────────────────────────────────────────────────

@dataclass
class CycleRecord:
    asset: str
    timestamp_abertura: datetime
    timestamp_expiracao: datetime
    direcao_1: Optional[str] = None
    direcao_2: Optional[str] = None
    preco_entrada_1: Optional[float] = None
    preco_entrada_2: Optional[float] = None
    preco_fechamento: Optional[float] = None
    resultado_financeiro: float = 0.0
    id_ordem_1: Optional[str] = None
    id_ordem_2: Optional[str] = None
    payout_no_momento: float = 0.0
    status: str = "pending"            # Valores finais abaixo:
    motivo_sem_order2: Optional[str] = None

    # status possíveis:
    #   "double_win"   — ambos ganham (+$1.70)
    #   "single_win"   — um ganha (−$0.15)
    #   "single_order" — Ordem 2 não foi colocada
    #   "fail_order1"  — Ordem 1 rejeitada
    #   "skip_payout"  — payout < mínimo
    #   "skip_timing"  — sem tempo suficiente
    #   "skip_price"   — falha ao obter preço
    #   "incomplete"   — timeout no resultado


# ── Estratégia principal ──────────────────────────────────────────────────────

class RangeSniper:
    """
    Bot Farejador de Faixa.
    Executa ciclos contínuos de 1 minuto no ativo configurado.
    Roda em thread separada; UI lê self.cycles e self.stats para exibição.
    """

    def __init__(
        self,
        trade_manager: TradeManager,
        data_feed: DataFeed,
        config: RangeSniperConfig,
    ) -> None:
        self._tm = trade_manager
        self._feed = data_feed
        self._cfg = config
        self._running = False
        self._stop_event = threading.Event()
        self._cycles: list[CycleRecord] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    # ── Controle público ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="RangeSniper", daemon=True)
        self._thread.start()
        logger.info("RangeSniper iniciado — ativo=%s stake=%.2f payout_min=%.0f%%",
                    self._cfg.asset, self._cfg.stake, self._cfg.min_payout * 100)

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False
        logger.info("RangeSniper: parada solicitada")

    @property
    def is_running(self) -> bool:
        return self._running

    def get_cycles(self) -> list[CycleRecord]:
        with self._lock:
            return list(self._cycles)

    def get_stats(self) -> dict:
        with self._lock:
            cycles = list(self._cycles)

        completed = [c for c in cycles if c.status not in (
            "pending", "skip_payout", "skip_timing", "skip_price"
        )]
        two_order = [c for c in completed if c.id_ordem_2 is not None]

        double_wins  = sum(1 for c in two_order if c.status == "double_win")
        single_wins  = sum(1 for c in two_order if c.status == "single_win")
        total_pnl    = sum(c.resultado_financeiro for c in completed)
        win_rate     = double_wins / len(two_order) if two_order else 0.0

        return {
            "total_ciclos":     len(cycles),
            "com_duas_ordens":  len(two_order),
            "duplo_ganho":      double_wins,
            "ganho_simples":    single_wins,
            "pnl_total":        round(total_pnl, 4),
            "taxa_duplo_ganho": round(win_rate * 100, 1),
            "skip_payout":      sum(1 for c in cycles if c.status == "skip_payout"),
        }

    # ── Loop interno ─────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_cycle()
            except Exception as exc:
                logger.error("Erro no ciclo: %s", exc, exc_info=True)
                self._stop_event.wait(timeout=5)
        self._running = False
        logger.info("RangeSniper: thread encerrada")

    def _run_cycle(self) -> None:
        """Executa um ciclo completo da janela de 1 minuto."""

        # ── Passo 1: Alinhar ao próximo limite de minuto ──────────────────────
        t3 = self._tm.next_minute_boundary()

        # Aguarda o início do novo candle (~1s após o limite anterior)
        wait_for_candle_open = t3 - time.time() - 58
        if wait_for_candle_open > 0:
            if self._stop_event.wait(timeout=wait_for_candle_open):
                return

        # Recalcula t3 para o candle atual
        t3 = math.ceil(time.time() / 60) * 60
        t3_dt = datetime.fromtimestamp(t3, tz=timezone.utc)

        cycle = CycleRecord(
            asset=self._cfg.asset,
            timestamp_abertura=datetime.now(tz=timezone.utc),
            timestamp_expiracao=t3_dt,
        )

        # ── Passo 2: Verificar payout ─────────────────────────────────────────
        payout = self._feed.get_payout(self._cfg.asset)
        cycle.payout_no_momento = payout

        if payout < self._cfg.min_payout:
            logger.info("Payout %.1f%% < mínimo %.1f%% — ciclo ignorado",
                        payout * 100, self._cfg.min_payout * 100)
            cycle.status = "skip_payout"
            self._append_cycle(cycle)
            return

        # ── Passo 3: Verificar tempo disponível ───────────────────────────────
        seconds_left = self._tm.seconds_until(t3)
        min_needed = self._cfg.entry_wait_seconds + self._cfg.min_seconds_to_expiry + 10

        if seconds_left < min_needed:
            logger.info("Tempo insuficiente (%.0fs < %ds) — aguardando próximo candle",
                        seconds_left, min_needed)
            cycle.status = "skip_timing"
            self._append_cycle(cycle)
            self._stop_event.wait(timeout=max(0.0, seconds_left + 2))
            return

        # ── Passo 4: Aguardar direção inicial do candle ───────────────────────
        candle_open = self._feed.get_candle_open(self._cfg.asset)
        if self._stop_event.wait(timeout=self._cfg.entry_wait_seconds):
            return

        current_price = self._feed.get_current_price(self._cfg.asset)
        if current_price is None or candle_open is None:
            logger.warning("Preço indisponível — ciclo ignorado")
            cycle.status = "skip_price"
            self._append_cycle(cycle)
            return

        # ── Passo 5: Determinar direção da Ordem 1 ────────────────────────────
        if current_price >= candle_open:
            order1_dir   = "call"
            trigger_fn   = lambda p: p >= candle_open + self._cfg.pip_distance
            order2_dir   = "put"
        else:
            order1_dir   = "put"
            trigger_fn   = lambda p: p <= candle_open - self._cfg.pip_distance
            order2_dir   = "call"

        # ── Passo 6: Colocar Ordem 1 ──────────────────────────────────────────
        order1_id = self._tm.place_trade(
            self._cfg.asset, self._cfg.stake, order1_dir, t3
        )
        if order1_id is None:
            logger.error("Ordem 1 rejeitada — abortando ciclo")
            cycle.status = "fail_order1"
            self._append_cycle(cycle)
            # Aguarda fim da janela para não criar inconsistência
            self._stop_event.wait(timeout=max(0.0, self._tm.seconds_until(t3) + 2))
            return

        cycle.id_ordem_1    = order1_id
        cycle.direcao_1     = order1_dir
        cycle.preco_entrada_1 = current_price
        logger.info("Ordem 1: %s em %.5f | id=%s | %.0fs até expiração",
                    order1_dir.upper(), current_price, order1_id, self._tm.seconds_until(t3))

        # ── Passo 7: Aguardar trigger e colocar Ordem 2 ───────────────────────
        order2_id = self._wait_for_trigger_and_place_order2(
            cycle, order2_dir, trigger_fn, t3
        )

        # ── Passo 8: Aguardar expiração ───────────────────────────────────────
        wait_for_expiry = self._tm.seconds_until(t3) + 1.5
        self._stop_event.wait(timeout=max(0.0, wait_for_expiry))

        # ── Passo 9: Coletar resultados ───────────────────────────────────────
        result1 = self._tm.wait_for_result(order1_id, t3, self._cfg.stake, order1_dir)
        result2 = (
            self._tm.wait_for_result(order2_id, t3, self._cfg.stake, order2_dir)
            if order2_id else None
        )

        # ── Passo 10: Calcular P&L e classificar ciclo ────────────────────────
        pnl = 0.0
        if result1:
            pnl += result1.profit
        if result2:
            pnl += result2.profit

        cycle.resultado_financeiro = pnl
        cycle.status = self._classify(result1, result2)
        self._append_cycle(cycle)

        logger.info(
            "Ciclo finalizado: %s | P&L=%.4f | O1=%s | O2=%s",
            cycle.status,
            pnl,
            result1.status if result1 else "N/A",
            result2.status if result2 else "N/A",
        )

    def _wait_for_trigger_and_place_order2(
        self,
        cycle: CycleRecord,
        direction: str,
        trigger_fn,
        t3: float,
    ) -> Optional[str]:
        """
        Monitora o preço até:
          - Trigger atingido → coloca Ordem 2
          - Menos de min_seconds_to_expiry restantes → aborta (Edge case C)
          - Payout caiu abaixo do mínimo → aborta (Edge case B)
          - Stop event → aborta
        """
        while True:
            seconds_left = self._tm.seconds_until(t3)

            # Edge case C: tempo insuficiente
            if seconds_left < self._cfg.min_seconds_to_expiry:
                logger.info("Ordem 2 cancelada: %.0fs restantes < mínimo %ds",
                            seconds_left, self._cfg.min_seconds_to_expiry)
                cycle.motivo_sem_order2 = "timeout"
                return None

            if self._stop_event.is_set():
                cycle.motivo_sem_order2 = "stop_requested"
                return None

            # Leitura de preço
            current_price = self._feed.get_current_price(self._cfg.asset)
            if current_price is None:
                time.sleep(self._cfg.price_poll_interval)
                continue

            # Edge case B: verificar payout novamente
            payout = self._feed.get_payout(self._cfg.asset)
            if payout < self._cfg.min_payout:
                logger.warning("Payout caiu para %.1f%% — Ordem 2 abortada", payout * 100)
                cycle.motivo_sem_order2 = "payout_dropped"
                return None

            # Verificar trigger
            if trigger_fn(current_price):
                order2_id = self._tm.place_trade(
                    self._cfg.asset, self._cfg.stake, direction, t3
                )
                if order2_id is None:
                    # Edge case A: Ordem 2 rejeitada
                    logger.warning("Ordem 2 rejeitada pela plataforma (Edge case A)")
                    cycle.motivo_sem_order2 = "rejection"
                    return None

                cycle.id_ordem_2      = order2_id
                cycle.direcao_2       = direction
                cycle.preco_entrada_2 = current_price
                logger.info("Ordem 2: %s em %.5f | id=%s | %.0fs até expiração",
                            direction.upper(), current_price, order2_id, seconds_left)
                return order2_id

            time.sleep(self._cfg.price_poll_interval)

    @staticmethod
    def _classify(
        r1: Optional[TradeResult],
        r2: Optional[TradeResult],
    ) -> str:
        if r1 is None:
            return "incomplete"
        if r2 is None:
            return "single_order"

        w1 = r1.status == "win"
        w2 = r2.status == "win"

        if w1 and w2:
            return "double_win"
        if w1 or w2:
            return "single_win"
        # Ambos perdendo é teoricamente impossível com P1≠P2,
        # mas pode ocorrer se os preços de entrada forem iguais (spread zerado)
        return "double_loss"

    def _append_cycle(self, cycle: CycleRecord) -> None:
        with self._lock:
            self._cycles.append(cycle)
