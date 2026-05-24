"""
Bot Runner — processo principal do Range Sniper.

Fluxo:
  1. Conecta à PocketOption via SSID
  2. Inicializa banco de dados PostgreSQL
  3. Lê configuração da tabela `configuracoes`
  4. Aguarda status_bot = "ativo" (atualizado pela UI Streamlit)
  5. Executa ciclos do Range Sniper em loop
  6. Persiste cada CycleRecord em `ciclos_operacao`

Tratamento de desconexão (edge case D):
  Qualquer exceção de WebSocket causa reconexão automática via connector.reconnect()
  e re-leitura da configuração antes de retomar.
"""
import os
import time
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/bot_runner.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("bot_runner")

from src.pocket_option.connector import PocketOptionConnector
from src.pocket_option.data_feed import DataFeed
from src.pocket_option.trade_manager import TradeManager
from src.strategies.range_sniper import RangeSniper, RangeSniperConfig, CycleRecord
from src.db.session import init_db, get_session
from src.db.models import Configuracao, CicloOperacao


CONFIG_POLL_INTERVAL = 10  # segundos entre leituras de status_bot


def load_config(session) -> Configuracao:
    """Carrega (ou cria) a configuração mais recente do banco."""
    cfg = session.query(Configuracao).order_by(Configuracao.id.desc()).first()
    if cfg is None:
        cfg = Configuracao()
        session.add(cfg)
        session.commit()
    return cfg


def persist_cycle(cycle: CycleRecord) -> None:
    """Salva um CycleRecord no banco de dados."""
    with get_session() as session:
        row = CicloOperacao(
            ativo=cycle.asset,
            id_ordem_1=cycle.id_ordem_1,
            id_ordem_2=cycle.id_ordem_2,
            timestamp_abertura=cycle.timestamp_abertura,
            timestamp_expiracao=cycle.timestamp_expiracao,
            direcao_1=cycle.direcao_1,
            direcao_2=cycle.direcao_2,
            preco_entrada_1=cycle.preco_entrada_1,
            preco_entrada_2=cycle.preco_entrada_2,
            resultado_financeiro=cycle.resultado_financeiro,
            payout_no_momento=cycle.payout_no_momento,
            status=cycle.status,
            motivo_sem_order2=cycle.motivo_sem_order2,
        )
        session.add(row)


def build_sniper_config(db_cfg: Configuracao) -> RangeSniperConfig:
    return RangeSniperConfig(
        asset=db_cfg.ativo,
        stake=db_cfg.valor_entrada,
        min_payout=db_cfg.payout_minimo,
        pip_distance=db_cfg.pip_distance,
        entry_wait_seconds=db_cfg.entry_wait_seconds,
        min_seconds_to_expiry=db_cfg.min_seconds_restantes,
    )


def main() -> None:
    logger.info("=== PocketOptionTrader — Bot Runner iniciando ===")

    # Inicializa banco de dados
    init_db()
    logger.info("Banco de dados pronto")

    # Conecta à PocketOption
    connector = PocketOptionConnector()
    connector.connect()

    feed = DataFeed(connector)
    trade_manager = TradeManager(connector)
    sniper: RangeSniper | None = None
    last_config_id: int = -1

    while True:
        try:
            # Lê configuração e status
            with get_session() as session:
                db_cfg = load_config(session)
                status = db_cfg.status_bot
                cfg_id = db_cfg.id

            if status != "ativo":
                if sniper and sniper.is_running:
                    sniper.stop()
                    logger.info("Bot parado via UI")
                time.sleep(CONFIG_POLL_INTERVAL)
                continue

            # Recria sniper se configuração mudou
            if cfg_id != last_config_id:
                if sniper and sniper.is_running:
                    sniper.stop()
                    time.sleep(2)

                sniper_cfg = build_sniper_config(db_cfg)
                sniper = RangeSniper(trade_manager, feed, sniper_cfg)
                last_config_id = cfg_id
                logger.info("Sniper configurado: %s", sniper_cfg)

            if not sniper.is_running:
                sniper.start()

            # Persiste novos ciclos completados
            completed = [c for c in sniper.get_cycles() if c.status != "pending"]
            if completed:
                for cycle in completed[-5:]:  # últimos 5 a cada poll
                    persist_cycle(cycle)

            time.sleep(CONFIG_POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Interrupção manual — encerrando")
            if sniper:
                sniper.stop()
            break

        except Exception as exc:
            logger.error("Erro no loop principal: %s", exc, exc_info=True)
            # Edge case D: reconectar WebSocket
            try:
                connector.reconnect()
            except Exception as reconn_exc:
                logger.error("Reconexão falhou: %s", reconn_exc)
            time.sleep(15)


if __name__ == "__main__":
    main()
