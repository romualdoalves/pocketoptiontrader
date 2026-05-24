"""
Painel de Controle — PocketOption Range Sniper
UI Streamlit — lê/escreve no PostgreSQL para controlar o bot_runner.

Acesso: https://pocketoption.tradixio.com  (produção)
        http://localhost:8501               (local)
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

# ── Página ────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Range Sniper — PocketOption",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# CSS dark trading — alinhado ao tema do AlpacaTrader
st.markdown("""
<style>
  /* Fundo principal */
  .stApp { background-color: #0E1117; }

  /* Sidebar */
  [data-testid="stSidebar"] { background-color: #1A1D27; border-right: 1px solid #2D3148; }

  /* Métricas */
  [data-testid="metric-container"] {
    background-color: #1A1D27;
    border: 1px solid #2D3148;
    border-radius: 8px;
    padding: 12px 16px;
  }
  [data-testid="metric-container"] label { color: #8B92A8 !important; font-size: 12px; }
  [data-testid="metric-container"] [data-testid="stMetricValue"] {
    color: #E8EAF0 !important; font-size: 26px; font-weight: 700;
  }

  /* Botão INICIAR */
  .stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #00D4AA, #0099CC);
    color: #0E1117; font-weight: 700; border: none; border-radius: 6px;
  }
  .stButton > button[kind="primary"]:hover { opacity: 0.88; }

  /* Botão PARAR */
  .stButton > button[kind="secondary"] {
    background-color: #2D1A1A; color: #FF6B6B;
    border: 1px solid #FF6B6B; border-radius: 6px;
  }

  /* Divisores */
  hr { border-color: #2D3148 !important; }

  /* Título principal */
  h1 { color: #00D4AA !important; letter-spacing: 1px; }
  h2, h3 { color: #C8CADB !important; }

  /* Status badge */
  .status-active {
    display:inline-block; padding:4px 12px; border-radius:20px;
    background:#0D3D2E; color:#00D4AA; border:1px solid #00D4AA;
    font-weight:600; font-size:13px;
  }
  .status-inactive {
    display:inline-block; padding:4px 12px; border-radius:20px;
    background:#3D0D0D; color:#FF6B6B; border:1px solid #FF6B6B;
    font-weight:600; font-size:13px;
  }
</style>
""", unsafe_allow_html=True)

# ── DB init ───────────────────────────────────────────────────────────────────

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


def get_recent_cycles(limit: int = 200) -> list[dict]:
    with get_session() as session:
        rows = (
            session.query(CicloOperacao)
            .order_by(CicloOperacao.id.desc())
            .limit(limit)
            .all()
        )
        return [r.to_dict() for r in rows]


def compute_stats(cycles: list[dict]) -> dict:
    # Retorno base com TODAS as chaves — evita KeyError quando vazio
    empty = {
        "total": 0, "com_2_ordens": 0,
        "duplo_ganho": 0, "ganho_simples": 0,
        "pnl": 0.0, "win_rate": 0.0, "skip_payout": 0,
    }
    if not cycles:
        return empty

    two_order  = [c for c in cycles if c.get("id_ordem_2")]
    dw         = sum(1 for c in two_order if c.get("status") == "double_win")
    sw         = sum(1 for c in two_order if c.get("status") == "single_win")
    pnl        = sum(c.get("resultado_financeiro") or 0 for c in cycles)
    wr         = dw / len(two_order) * 100 if two_order else 0.0
    skipped    = sum(1 for c in cycles if c.get("status") == "skip_payout")

    return {
        "total":        len(cycles),
        "com_2_ordens": len(two_order),
        "duplo_ganho":  dw,
        "ganho_simples":sw,
        "pnl":          round(pnl, 4),
        "win_rate":     round(wr, 1),
        "skip_payout":  skipped,
    }


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 🎯 Range Sniper")
    st.caption("PocketOption — Bot Farejador de Faixa")
    st.divider()

    cfg = get_config()

    # ── Status ────────────────────────────────────────────────────────────────
    is_active = cfg.status_bot == "ativo"
    badge = '<span class="status-active">● ATIVO</span>' if is_active else '<span class="status-inactive">● INATIVO</span>'
    st.markdown(badge, unsafe_allow_html=True)
    st.markdown("")

    if is_active:
        if st.button("⏹  PARAR BOT", use_container_width=True, type="secondary"):
            update_config(status_bot="inativo")
            st.rerun()
    else:
        if st.button("▶  INICIAR BOT", use_container_width=True, type="primary"):
            update_config(status_bot="ativo")
            st.rerun()

    st.divider()

    # ── Configuração ──────────────────────────────────────────────────────────
    st.subheader("Configuração")

    _ativos = ["EURUSD_otc", "EURUSD", "GBPUSD_otc", "GBPUSD",
               "USDJPY_otc", "BTCUSD_otc", "ETHUSD_otc"]
    ativo = st.selectbox(
        "Ativo",
        _ativos,
        index=_ativos.index(cfg.ativo) if cfg.ativo in _ativos else 0,
        help="Par OTC disponível 24/7, incluindo fins de semana",
    )

    stake = st.number_input(
        "Valor por ordem (USD)",
        min_value=1.0, max_value=1000.0,
        value=float(cfg.valor_entrada), step=1.0,
    )

    payout_min = st.slider(
        "Payout mínimo (%)", 70, 95,
        value=int(cfg.payout_minimo * 100),
        help="O bot só opera se o payout ≥ este valor",
    )

    pip_distance = st.number_input(
        "Trigger Ordem 2 (pips)", 1, 20,
        value=int(round(cfg.pip_distance / 0.0001)), step=1,
        help="Movimento mínimo (pips) para ativar a 2ª ordem",
    )

    entry_wait = st.number_input(
        "Aguardar após abertura (s)", 5, 30,
        value=int(cfg.entry_wait_seconds), step=1,
    )

    min_seconds = st.number_input(
        "Mín. segundos até expiração", 10, 30,
        value=int(cfg.min_seconds_restantes), step=1,
        help="Edge case C: não coloca Ordem 2 se restar < este valor",
    )

    if st.button("💾 Salvar", use_container_width=True):
        update_config(
            ativo=ativo,
            valor_entrada=stake,
            payout_minimo=payout_min / 100.0,
            pip_distance=pip_distance * 0.0001,
            entry_wait_seconds=entry_wait,
            min_seconds_restantes=min_seconds,
        )
        st.success("Configuração salva!")
        time.sleep(0.4)
        st.rerun()

    st.divider()
    demo_label = "DEMO" if os.environ.get("POCKET_DEMO", "1") == "1" else "🔴 REAL"
    st.caption(f"Conta: **{demo_label}**")
    st.caption(f"UID: {os.environ.get('POCKET_UID', 'N/A')}")
    st.caption(f"Ativo: {cfg.ativo}")


# ── Dashboard principal ───────────────────────────────────────────────────────

st.markdown("# 📊 Dashboard — Range Sniper")

cycles = get_recent_cycles(200)
stats  = compute_stats(cycles)

# ── Métricas de topo ──────────────────────────────────────────────────────────
col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Total de ciclos",   stats["total"])
col2.metric("Com 2 ordens",      stats["com_2_ordens"])
col3.metric("🎯 Duplo ganho",    stats["duplo_ganho"])
col4.metric("Ganho simples",     stats["ganho_simples"])

pnl = stats["pnl"]
col5.metric(
    "P&L Total (USD)",
    f"${pnl:.2f}",
    delta=f"{pnl:+.2f}" if pnl != 0 else None,
    delta_color="normal",
)

st.divider()

# ── Linha de análise ──────────────────────────────────────────────────────────
col_a, col_b, col_c, col_d = st.columns(4)

payout_atual = cfg.payout_minimo
breakeven    = round(0.15 / (0.15 + payout_atual) * 100, 1)

col_a.metric("Payout mínimo config.", f"{payout_atual*100:.0f}%")
col_b.metric("Win rate atual",        f"{stats['win_rate']:.1f}%")
col_c.metric("Break-even necessário", f"{breakeven:.1f}%",
             help="% mínima de ciclos 'duplo ganho' para lucro")
col_d.metric("Skip payout",           stats["skip_payout"],
             help="Ciclos ignorados por payout abaixo do mínimo")

st.divider()

# ── Tabela de ciclos ──────────────────────────────────────────────────────────

st.subheader("Ciclos Recentes")

if cycles:
    df = pd.DataFrame(cycles)
    cols_show = [
        "id", "ativo", "timestamp_abertura",
        "direcao_1", "direcao_2",
        "preco_entrada_1", "preco_entrada_2",
        "resultado_financeiro", "payout_no_momento",
        "status", "motivo_sem_order2",
    ]
    cols_show = [c for c in cols_show if c in df.columns]
    df_show = df[cols_show].head(50)

    COLOR_MAP = {
        "double_win":   "background-color:#0D3D2E; color:#00D4AA",
        "single_win":   "background-color:#0D2A3D; color:#5BB8FF",
        "single_order": "background-color:#2A2D10; color:#D4CC00",
        "double_loss":  "background-color:#3D0D0D; color:#FF6B6B",
        "skip_payout":  "background-color:#1A1D27; color:#555870",
        "skip_timing":  "background-color:#1A1D27; color:#555870",
        "fail_order1":  "background-color:#3D1E00; color:#FF9944",
    }

    def color_status(val):
        return COLOR_MAP.get(str(val), "")

    styled = df_show.style.applymap(color_status, subset=["status"])
    st.dataframe(styled, use_container_width=True, height=460)
else:
    st.info("Nenhum ciclo registrado ainda. Inicie o bot para começar.")

# ── Gráfico P&L acumulado ─────────────────────────────────────────────────────

if cycles:
    df_pnl = pd.DataFrame(cycles)
    if {"resultado_financeiro", "timestamp_abertura"}.issubset(df_pnl.columns):
        df_pnl = (
            df_pnl[["timestamp_abertura", "resultado_financeiro"]]
            .dropna()
            .copy()
        )
        df_pnl["timestamp_abertura"] = pd.to_datetime(df_pnl["timestamp_abertura"])
        df_pnl = df_pnl.sort_values("timestamp_abertura")
        df_pnl["P&L Acumulado (USD)"] = df_pnl["resultado_financeiro"].cumsum()

        st.subheader("P&L Acumulado")
        st.line_chart(
            df_pnl.set_index("timestamp_abertura")["P&L Acumulado (USD)"],
            color="#00D4AA",
        )

# ── Distribuição ──────────────────────────────────────────────────────────────

if cycles:
    df_status = pd.DataFrame(cycles)
    if "status" in df_status.columns:
        st.subheader("Distribuição de Resultados")
        counts = df_status["status"].value_counts().reset_index()
        counts.columns = ["Status", "Contagem"]
        st.bar_chart(counts.set_index("Status"))

# ── Rodapé ────────────────────────────────────────────────────────────────────

st.divider()
col_r1, col_r2 = st.columns([4, 1])
col_r1.caption(f"Última atualização: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
with col_r2:
    if st.button("🔄 Atualizar"):
        st.rerun()
