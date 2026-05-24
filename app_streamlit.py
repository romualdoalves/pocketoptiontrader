"""
Painel de Controle — PocketOption Range Sniper
UI Streamlit — lê/escreve no PostgreSQL para controlar o bot_runner.

Acesso: http://pocketoption.tradixio.com  (produção)
        http://localhost:8501              (local)
"""
import os
import time
import logging
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from src.db.session import init_db, get_session
from src.db.models import Configuracao, CicloOperacao

logging.basicConfig(level=logging.WARNING)

# ── Configuração da página ────────────────────────────────────────────────────

st.set_page_config(
    page_title="PocketOption Range Sniper",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Inicializa DB na primeira execução
try:
    init_db()
except Exception as exc:
    st.error(f"Erro ao conectar no banco de dados: {exc}")
    st.stop()


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_config() -> Configuracao:
    with get_session() as session:
        cfg = session.query(Configuracao).order_by(Configuracao.id.desc()).first()
        if cfg is None:
            cfg = Configuracao()
            session.add(cfg)
            session.commit()
        return cfg


def update_config(**kwargs) -> None:
    with get_session() as session:
        cfg = session.query(Configuracao).order_by(Configuracao.id.desc()).first()
        for k, v in kwargs.items():
            setattr(cfg, k, v)


def get_recent_cycles(limit: int = 100) -> list[dict]:
    with get_session() as session:
        rows = (
            session.query(CicloOperacao)
            .order_by(CicloOperacao.id.desc())
            .limit(limit)
            .all()
        )
        return [r.to_dict() for r in rows]


def compute_stats(cycles: list[dict]) -> dict:
    if not cycles:
        return {"total": 0, "duplo_ganho": 0, "ganho_simples": 0, "pnl": 0.0, "win_rate": 0.0}

    two_order = [c for c in cycles if c.get("id_ordem_2")]
    dw = sum(1 for c in two_order if c["status"] == "double_win")
    sw = sum(1 for c in two_order if c["status"] == "single_win")
    pnl = sum(c.get("resultado_financeiro", 0) or 0 for c in cycles)
    wr = dw / len(two_order) * 100 if two_order else 0.0

    return {
        "total":        len(cycles),
        "com_2_ordens": len(two_order),
        "duplo_ganho":  dw,
        "ganho_simples":sw,
        "pnl":          round(pnl, 4),
        "win_rate":     round(wr, 1),
    }


# ── Sidebar — Configuração ───────────────────────────────────────────────────

with st.sidebar:
    st.title("🎯 Range Sniper")
    st.caption("PocketOption — Bot Farejador de Faixa")
    st.divider()

    cfg = get_config()

    st.subheader("Configuração")

    ativo = st.selectbox(
        "Ativo",
        ["EURUSD_otc", "EURUSD", "GBPUSD_otc", "GBPUSD", "USDJPY_otc",
         "BTCUSD_otc", "ETHUSD_otc"],
        index=["EURUSD_otc", "EURUSD", "GBPUSD_otc", "GBPUSD", "USDJPY_otc",
               "BTCUSD_otc", "ETHUSD_otc"].index(cfg.ativo)
        if cfg.ativo in ["EURUSD_otc", "EURUSD", "GBPUSD_otc", "GBPUSD",
                          "USDJPY_otc", "BTCUSD_otc", "ETHUSD_otc"] else 0,
        help="Par OTC disponível 24/7, incluindo fins de semana",
    )

    stake = st.number_input(
        "Valor por ordem (USD)",
        min_value=1.0, max_value=1000.0, value=float(cfg.valor_entrada),
        step=1.0,
    )

    payout_min = st.slider(
        "Payout mínimo (%)",
        min_value=70, max_value=95, value=int(cfg.payout_minimo * 100),
        help="O bot só opera se o payout atual for ≥ este valor",
    )

    pip_distance = st.number_input(
        "Distância trigger Ordem 2 (pips)",
        min_value=1, max_value=20,
        value=int(round(cfg.pip_distance / 0.0001)),
        step=1,
        help="Quantos pips o preço deve se mover para ativar a 2ª ordem",
    )

    entry_wait = st.number_input(
        "Aguardar após abertura do candle (s)",
        min_value=5, max_value=30, value=int(cfg.entry_wait_seconds), step=1,
    )

    min_seconds = st.number_input(
        "Mín. segundos para expiração (edge case C)",
        min_value=10, max_value=30, value=int(cfg.min_seconds_restantes), step=1,
    )

    if st.button("💾 Salvar configuração"):
        update_config(
            ativo=ativo,
            valor_entrada=stake,
            payout_minimo=payout_min / 100.0,
            pip_distance=pip_distance * 0.0001,
            entry_wait_seconds=entry_wait,
            min_seconds_restantes=min_seconds,
        )
        st.success("Configuração salva!")
        time.sleep(0.5)
        st.rerun()

    st.divider()

    # ── Controle Start/Stop ───────────────────────────────────────────────────
    is_active = cfg.status_bot == "ativo"

    if is_active:
        st.success("🟢 Bot ATIVO")
        if st.button("⏹ PARAR BOT", use_container_width=True, type="secondary"):
            update_config(status_bot="inativo")
            st.rerun()
    else:
        st.error("🔴 Bot INATIVO")
        if st.button("▶ INICIAR BOT", use_container_width=True, type="primary"):
            update_config(status_bot="ativo")
            st.rerun()

    st.divider()
    st.caption(f"Conta: {'DEMO' if os.environ.get('POCKET_DEMO','1')=='1' else '🔴 REAL'}")
    st.caption(f"UID: {os.environ.get('POCKET_UID', 'N/A')}")


# ── Página principal ──────────────────────────────────────────────────────────

st.title("📊 Dashboard — Range Sniper")

cycles = get_recent_cycles(200)
stats = compute_stats(cycles)

# Métricas de topo
col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Total de ciclos", stats["total"])
col2.metric("Com 2 ordens",    stats["com_2_ordens"])
col3.metric("Duplo ganho 🎯",  stats["duplo_ganho"])
col4.metric("Ganho simples",   stats["ganho_simples"])
col5.metric("P&L Total (USD)", f"${stats['pnl']:.2f}",
            delta=f"{stats['pnl']:+.2f}" if stats['pnl'] else None)

st.divider()

# Break-even info
col_a, col_b, col_c = st.columns(3)
payout_atual = cfg.payout_minimo
breakeven = 0.15 / 1.85 * 100  # 8.1% com payout 85%
col_a.metric("Payout configurado", f"{payout_atual*100:.0f}%")
col_b.metric("Win rate atual", f"{stats['win_rate']:.1f}%")
col_c.metric("Break-even necessário", f"{breakeven:.1f}%",
             help="% mínima de ciclos com duplo ganho para ser lucrativo")

st.divider()

# ── Tabela de ciclos recentes ─────────────────────────────────────────────────

st.subheader("Ciclos Recentes")

if cycles:
    df = pd.DataFrame(cycles)

    # Formatar colunas
    cols_show = [
        "id", "ativo", "timestamp_abertura", "direcao_1", "direcao_2",
        "preco_entrada_1", "preco_entrada_2",
        "resultado_financeiro", "payout_no_momento", "status", "motivo_sem_order2",
    ]
    cols_show = [c for c in cols_show if c in df.columns]
    df = df[cols_show].head(50)

    # Color coding por status
    def color_status(val):
        colors = {
            "double_win":   "background-color: #1a5c1a; color: white",
            "single_win":   "background-color: #1a3a5c; color: white",
            "single_order": "background-color: #3a3a1a; color: white",
            "double_loss":  "background-color: #5c1a1a; color: white",
            "skip_payout":  "background-color: #2a2a2a; color: #888",
            "skip_timing":  "background-color: #2a2a2a; color: #888",
            "fail_order1":  "background-color: #5c2a00; color: white",
        }
        return colors.get(val, "")

    styled = df.style.applymap(color_status, subset=["status"])
    st.dataframe(styled, use_container_width=True, height=500)
else:
    st.info("Nenhum ciclo registrado ainda. Inicie o bot para começar.")

# ── Gráfico de P&L acumulado ──────────────────────────────────────────────────

if cycles:
    st.subheader("P&L Acumulado")
    df_pnl = pd.DataFrame(cycles)
    if "resultado_financeiro" in df_pnl.columns and "timestamp_abertura" in df_pnl.columns:
        df_pnl = df_pnl[["timestamp_abertura", "resultado_financeiro"]].dropna()
        df_pnl["timestamp_abertura"] = pd.to_datetime(df_pnl["timestamp_abertura"])
        df_pnl = df_pnl.sort_values("timestamp_abertura")
        df_pnl["pnl_acumulado"] = df_pnl["resultado_financeiro"].cumsum()
        st.line_chart(df_pnl.set_index("timestamp_abertura")["pnl_acumulado"])

# ── Distribuição de status ────────────────────────────────────────────────────

if cycles:
    st.subheader("Distribuição de Resultados")
    df_status = pd.DataFrame(cycles)
    if "status" in df_status.columns:
        counts = df_status["status"].value_counts()
        st.bar_chart(counts)

# Auto-refresh a cada 30s
st.caption(f"Última atualização: {datetime.now(tz=timezone.utc).strftime('%H:%M:%S UTC')}")
if st.button("🔄 Atualizar"):
    st.rerun()
