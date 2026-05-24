"""
Modelos SQLAlchemy — banco de dados PostgreSQL `pocketoption`.

Entidades do PRD:
  Configuracao  — parâmetros do bot (payout_minimo, stake, ativo, status)
  CicloOperacao — log de cada par de ordens executado
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, Float, String, DateTime, Text, func
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Configuracao(Base):
    """Configuração ativa do bot. Atualizada pela UI Streamlit."""
    __tablename__ = "configuracoes"

    id                     = Column(Integer, primary_key=True, autoincrement=True)
    payout_minimo          = Column(Float,   nullable=False, default=0.85)
    valor_entrada          = Column(Float,   nullable=False, default=1.0)
    ativo                  = Column(String(30), nullable=False, default="EURUSD_otc")
    status_bot             = Column(String(20), nullable=False, default="inativo")
    pip_distance           = Column(Float,   nullable=False, default=0.0003)
    entry_wait_seconds     = Column(Integer, nullable=False, default=10)
    min_seconds_restantes  = Column(Integer, nullable=False, default=15)
    atualizado_em          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":                    self.id,
            "payout_minimo":         self.payout_minimo,
            "valor_entrada":         self.valor_entrada,
            "ativo":                 self.ativo,
            "status_bot":            self.status_bot,
            "pip_distance":          self.pip_distance,
            "entry_wait_seconds":    self.entry_wait_seconds,
            "min_seconds_restantes": self.min_seconds_restantes,
        }


class BotStatus(Base):
    """
    Estado em tempo real do bot — escrito pelo bot_runner a cada ciclo.
    Lido pela UI Streamlit para exibir indicadores ao vivo.
    """
    __tablename__ = "bot_status"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    saldo           = Column(Float,   nullable=True)   # balance conta PocketOption
    saldo_inicial   = Column(Float,   nullable=True)   # saldo ao iniciar a sessão
    payout_atual    = Column(Float,   nullable=True)   # payout % do ativo configurado
    ultimo_preco    = Column(Float,   nullable=True)   # último preço do ativo
    status_conexao  = Column(String(20), default="desconectado")  # conectado/desconectado
    ciclos_sessao   = Column(Integer, default=0)       # ciclos desde último start
    pnl_sessao      = Column(Float,   default=0.0)     # P&L desde último start
    atualizado_em   = Column(DateTime, default=func.now(), onupdate=func.now())

    def to_dict(self) -> dict:
        return {
            "saldo":          self.saldo,
            "saldo_inicial":  self.saldo_inicial,
            "payout_atual":   self.payout_atual,
            "ultimo_preco":   self.ultimo_preco,
            "status_conexao": self.status_conexao,
            "ciclos_sessao":  self.ciclos_sessao,
            "pnl_sessao":     self.pnl_sessao,
            "atualizado_em":  self.atualizado_em.isoformat() if self.atualizado_em else None,
        }


class CicloOperacao(Base):
    """
    Registro de um ciclo completo (1 par de ordens ou ciclo ignorado).
    Persistido pelo bot_runner após cada ciclo.
    """
    __tablename__ = "ciclos_operacao"

    id                     = Column(Integer, primary_key=True, autoincrement=True)
    ativo                  = Column(String(30), nullable=False)
    id_ordem_1             = Column(String(100), nullable=True)
    id_ordem_2             = Column(String(100), nullable=True)
    timestamp_abertura     = Column(DateTime, nullable=False, default=datetime.utcnow)
    timestamp_expiracao    = Column(DateTime, nullable=True)
    direcao_1              = Column(String(10), nullable=True)   # "call" | "put"
    direcao_2              = Column(String(10), nullable=True)
    preco_entrada_1        = Column(Float, nullable=True)
    preco_entrada_2        = Column(Float, nullable=True)
    preco_fechamento       = Column(Float, nullable=True)
    resultado_financeiro   = Column(Float, nullable=False, default=0.0)
    payout_no_momento      = Column(Float, nullable=True)
    status                 = Column(String(30), nullable=False)
    motivo_sem_order2      = Column(Text, nullable=True)
    criado_em              = Column(DateTime, default=func.now())

    def to_dict(self) -> dict:
        return {
            "id":                   self.id,
            "ativo":                self.ativo,
            "id_ordem_1":           self.id_ordem_1,
            "id_ordem_2":           self.id_ordem_2,
            "timestamp_abertura":   self.timestamp_abertura.isoformat() if self.timestamp_abertura else None,
            "timestamp_expiracao":  self.timestamp_expiracao.isoformat() if self.timestamp_expiracao else None,
            "direcao_1":            self.direcao_1,
            "direcao_2":            self.direcao_2,
            "preco_entrada_1":      self.preco_entrada_1,
            "preco_entrada_2":      self.preco_entrada_2,
            "resultado_financeiro": self.resultado_financeiro,
            "payout_no_momento":    self.payout_no_momento,
            "status":               self.status,
            "motivo_sem_order2":    self.motivo_sem_order2,
        }
