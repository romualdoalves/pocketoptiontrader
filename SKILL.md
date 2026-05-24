---
name: pocketoptiontrader
description: |
  PocketOptionTrader - Bot de trading de opções binárias na plataforma PocketOption.

  Estratégia Principal: BOT FAREJADOR DE FAIXA (Range Sniper)
    - Coloca CALL + PUT na mesma janela de expiração de 1 minuto
    - "Zona de ganho duplo": preço entre P1 e P2 → ambas as ordens ganham
    - Ambas perdem é IMPOSSÍVEL matematicamente (P1 < P2)
    - Break-even: apenas 8.1% de ciclos com duplo ganho (payout 85%)
    - Ativo: EURUSD_otc | Stake: $1 | Expiração: 1 minuto

  Estratégias herdadas do Alpaca (mantidas):
    - Estratégia 2 "Liquidity Sentinel" — Volume Profile + Z-Score
    - Estratégia 3 "AI Allocator" — LSTM alpha, Kelly sizing
    - 25+ estratégias modulares no TraderDev backend

  Implementações:
    1. RANGE SNIPER (src/bot_runner.py + app_streamlit.py) — ATIVO
    2. TRADERDEV    (TraderDev/ — FastAPI + React + PostgreSQL) — em desenvolvimento

  Deploy:
    - Local: run_range_sniper.bat (Windows)
    - Produção: pocketoption.tradixio.com (VPS Hostinger KVM4, Docker + Traefik)
    - DB: PostgreSQL pocketoption / crypto / crypto

  Use este skill quando perguntar sobre: bot PocketOption, Range Sniper, Farejador
  de Faixa, sincronização de expiração, zona de ganho duplo, t1/t2/t3, EURUSD_otc,
  conector WebSocket, edge cases A/B/C/D, Streamlit UI, deploy VPS, Traefik.
---

# PocketOptionTrader — Documentação do Projeto

**Última atualização:** 2026-05-24  
**Conta:** PocketOption Demo (UID: 2197423) — conta real disponível  
**Localização:** Brasil → deploy: VPS Hostinger KVM4 (pocketoption.tradixio.com)  
**Repositório:** https://github.com/romualdoalves/pocketoptiontrader.git

---

## 1. Contexto: Migração Alpaca → PocketOption

Este projeto é a migração completa do AlpacaTrader (spot crypto, paper trading $100k)
para a plataforma PocketOption de opções binárias. A autenticação no PocketOption é
feita via Google Account e as credenciais são cookies de sessão extraídos do navegador.

### Diferenças fundamentais

| Aspecto | Alpaca (Spot) | PocketOption (Binário) |
|---------|--------------|------------------------|
| Tipo de ordem | Market/Limit/Stop | Call (alta) / Put (baixa) |
| Gestão de posição | Entry + SL + TP | Não existe — resultado na expiração |
| Sizing | Qty de ativos (vol-adjusted) | Valor USD por aposta (Kelly) |
| Saída | Manual ou SL/TP | Automática na expiração |
| Resultado | P&L variável | Fixo: +80-95% (win) ou -100% (loss) |
| Break-even | Qualquer win rate > 0 | >54% com payout 85% |
| Short | Desabilitado (spot) | **Habilitado via PUT** |
| Horário | Mercado aberto | 24/7 (OTC nos fins de semana) |

---

## 2. Credenciais PocketOption

Armazenadas exclusivamente no `.env` (nunca commitar):

```bash
POCKET_SSID=<cookie ci_session — identificador da sessão>
POCKET_UID=<ID do usuário>
POCKET_SECRET=<chave de autenticação>
POCKET_DEMO=1   # 1=conta demo, 0=conta real
```

Autenticação via WebSocket usando `pocketoptionapi`:
```python
from pocketoptionapi.stable_api import PocketOption
api = PocketOption(ssid=POCKET_SSID, demo=bool(int(POCKET_DEMO)))
await api.connect()
```

---

## 3. Estrutura de Arquivos

```text
PocketOptionTrader/
├── .env                               # Credenciais (nunca commitar)
├── .env.example                       # Template
├── SKILL.md                           # Esta documentação
├── PLAN.md                            # Plano detalhado de migração
├── requirements.txt
├── app_streamlit.py                   # Painel de controle (UI)
├── run_range_sniper.bat                # Lançamento Windows
├── docker-compose.yml                 # Stack: bot + streamlit + postgres
├── docker-compose.traefik.yml         # Overlay produção (VPS + Traefik)
├── Dockerfile.bot                     # Container do bot runner
├── Dockerfile.streamlit               # Container da UI
├── .github/workflows/ci.yml           # CI básico (lint + testes)
│
├── src/
│   ├── pocket_option/                 # Camada core PocketOption
│   │   ├── __init__.py
│   │   ├── connector.py               # WebSocket auth + lifecycle (SSID)
│   │   ├── data_feed.py               # Preço atual + candles OHLCV
│   │   └── trade_manager.py           # place_trade(), wait_for_result()
│   │
│   ├── strategies/
│   │   ├── __init__.py
│   │   └── range_sniper.py            # BOT FAREJADOR DE FAIXA (ATIVO)
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── models.py                  # Configuracao, CicloOperacao (SQLAlchemy)
│   │   └── session.py                 # get_session(), init_db()
│   │
│   ├── bot_runner.py                  # Processo principal do bot
│   ├── intraday_trader.py             # Estratégia 2 Alpaca (referência)
│   ├── trade_logger.py                # Persistência JSONL
│   ├── evolve.py                      # Level 4A — Evolution Strategies
│   ├── bayesian_optimize.py           # Level 4B — Optuna
│   ├── meta_optimizer.py              # Level 4D — Claude API meta-review
│   └── strategy3/                     # Estratégia 3 Alpaca (referência)
│
├── config/
│   ├── strategy_params.json
│   └── population.json
│
├── logs/
│   └── bot_runner.log
│
└── TraderDev/                         # UI full-stack (em desenvolvimento)
    └── backend/
        └── strategies/               # 25+ estratégias modulares
```

---

## 4. Estratégia Principal: Bot Farejador de Faixa (Range Sniper)

Fonte: `src/strategies/range_sniper.py` | Runner: `src/bot_runner.py`

### Matemática da Zona de Ganho Duplo

```
Payout 85%, stake $1/ordem, risco total $2 por ciclo:

  Ambos ganham (preço entre P1 e P2): +$0.85 × 2 = +$1.70  ✅
  Um ganha (preço > P2 ou < P1):      +$0.85 − $1 = −$0.15
  Ambos perdem:                        IMPOSSÍVEL (P1 < P2)

Break-even: P(ambos ganham) > 0.15 / 1.85 = 8.1%
```

### Fluxo do ciclo (janela de 1 minuto)

```
t0 → alinha ao início do novo candle M1
t1 → aguarda 10s (entry_wait_seconds), lê direção do candle
   → coloca Ordem 1 (CALL se preço subiu, PUT se caiu)
t1..t3-15s → monitora preço; quando atinge pip_distance (3 pips)
           → coloca Ordem 2 (direção oposta) com MESMO t3
t3 → ambas as ordens expiram no mesmo timestamp de minuto
```

### Edge Cases do PRD

| Caso | Cenário | Tratamento |
|------|---------|-----------|
| **A** | Ordem 2 rejeitada pela plataforma | Alerta, entra em espera até t3 |
| **B** | Payout caiu abaixo do mínimo antes de Ordem 2 | Aborta Ordem 2 |
| **C** | Menos de 15s para expiração | Bloqueia Ordem 2 |
| **D** | WebSocket desconectado | Congela, reconecta em loop exponencial |

### Sincronização de expiração (t3)

Na PocketOption, `expirations_mode=1` (1 minuto) expira no próximo limite
de minuto inteiro. Tanto Ordem 1 (t1) quanto Ordem 2 (t2), se colocadas
no mesmo minuto, expiram exatamente em t3 = próximo `:00`.

```python
t3 = math.ceil(time.time() / 60) * 60  # ex: 10:00:45 → t3 = 10:01:00
```

### Configuração padrão

```json
{
  "asset": "EURUSD_otc",
  "stake": 1.0,
  "min_payout": 0.85,
  "pip_distance": 0.0003,
  "entry_wait_seconds": 10,
  "min_seconds_to_expiry": 15
}
```

---

## 5. Estratégia 2: Liquidity Sentinel → Binário (5 fases)

Fonte: `src/po_intraday_trader.py` (lógica de sinal idêntica ao original).

### Fase 1 — Macro Filter
- Funding rate Binance (API pública): bloqueia CALL se > +0.01%/8h
- Open Interest trend: bloqueia CALL se preço sobe + OI cai

### Fase 2 — Volume Profile (VAH/POC/VAL)
- 50 bins, 60 candles (5h de M5)
- POC = nível de maior volume
- VAH/VAL = limites da Value Area (70% do volume)
- Regime: BALANCED (VAL ≤ preço ≤ VAH) ou IMBALANCED

### Fase 3 — Sinais de Entrada (dois motores)

**Engine A — IMBALANCED (breakout)**
- CALL: `preço > VAH` AND `delta_z >= 2.0` AND `preço > VWAP` AND `preço > EMA50`
- **PUT** (novo — PocketOption permite short): `preço < VAL` AND `delta_z <= -2.0` AND `preço < VWAP`

**Engine B — BALANCED (mean reversion)**
- CALL em VAL: `preço ≤ VAL + 0.3×ATR` AND (`MFI < 30` OR divergência bullish)
- PUT em VAH: `preço ≥ VAH - 0.3×ATR` AND (`MFI > 70` OR divergência bearish)

### Fase 4 — Sizing (Kelly Criterion)
```
payout = 0.85  (conservador)
f* = (win_rate × payout - (1 - win_rate)) / payout
bet = (f* / 2) × saldo   # ½ Kelly para segurança
bet = max(1.0, min(bet, saldo × 0.05))  # clamp [1 USD, 5% do saldo]
```

### Fase 5 — Safety Suite
- Expiração: **300 segundos (5 min)** — alinhado ao timeframe M5
- Horário: 24/7 (sem restrição de mercado — binário opera sempre)
- OTC nos fins de semana: pares `_OTC` (EURUSD_OTC, etc.)
- Circuit breakers (seção 6)

---

## 5. Estratégia 3: AI Allocator Multi-Asset (binário)

Fonte: `src/strategy3/strategy3_main.py` + `po_executor.py`

### Universo de Ativos
```python
CORE_ASSETS = ["EURUSD", "GBPUSD", "USDJPY", "BTCUSD", "ETHUSD", "XAUUSD"]
OTC_ASSETS  = ["EURUSD_OTC", "GBPUSD_OTC", "USDJPY_OTC"]  # fins de semana
```

### Fluxo do Ciclo (5 min)
1. **Macro Filter** — Binance funding rate + OI (inalterado)
2. **Regime de mercado** — % de ativos acima da EMA200 (breadth)
3. **LSTM Alpha** — Sinal LONG/SHORT/HOLD por ativo (inalterado)
4. **Kelly Allocation** — Tamanho da aposta por ativo baseado em win rate histórica
5. **Risk Gate** — Verificar drawdown tier + win rate circuit breaker
6. **Execução** — Para cada sinal, colocar call/put via `po_executor.py`

### Adaptações vs Alpaca
- **Sem rebalanceamento de portfólio** — cada aposta é independente
- **Kelly Criterion** por ativo (usa win rate das últimas 50 apostas no par)
- **Não há posição aberta** — cada aposta tem resultado em 5 min
- **Drawdown calculado sobre saldo** (igual ao original)

---

## 6. Gerenciamento de Risco

### 6.1 Sizing — Kelly Criterion
```
Break-even (payout 85%): win_rate > 54.05%
½ Kelly para crescimento sustentável:
  bet = 0.5 × [(wr × 0.85 - (1-wr)) / 0.85] × saldo
  Mínimo: $1 | Máximo: 5% do saldo
```

### 6.2 Circuit Breakers (em ordem de prioridade)
| Condição | Ação | Cooldown |
|----------|------|----------|
| 3 losses consecutivos | Parar | 30 min |
| Win rate < 50% / 20 apostas | Parar | 60 min |
| Perda diária > 15% do saldo | Parar | Próximo dia |
| Conexão perdida | Reconectar | Não apostar até sincronizar |
| Drawdown ≥ 6% | Flat | 24 horas |

### 6.3 Drawdown Tiers (herdado da Estratégia 3 original)
| DD | Exposição |
|----|-----------|
| 0-2% | 100% |
| 2-4% | 80% |
| 4-6% | 40% |
| ≥6% | 0% (flat) |

---

## 7. Deploy — VPS Hostinger (pocketoption.tradixio.com)

### Infraestrutura
- **VPS:** Hostinger KVM4
- **Docker + Traefik:** Pré-instalados
- **Subdomínio:** `pocketoption.tradixio.com`
- **TLS:** Let's Encrypt via Traefik (automático)
- **Path no VPS:** `/opt/pocketoption` (todos os projetos Hostinger em `/opt`)

### Deploy inicial no VPS
```bash
git clone https://github.com/romualdoalves/pocketoptiontrader.git /opt/pocketoption
cd /opt/pocketoption
cp .env.example .env && nano .env   # preencher credenciais PocketOption
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d --build
```

### Atualização
```bash
cd /opt/pocketoption && git pull
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d --build
```

### Logs em tempo real
```bash
docker compose logs -f bot        # bot runner
docker compose logs -f streamlit  # UI
```

### Labels Traefik (API FastAPI na porta 8000)
```yaml
traefik.enable=true
traefik.http.routers.potrader.rule=Host(`pocketoption.tradixio.com`)
traefik.http.routers.potrader.tls.certresolver=letsencrypt
```

---

## 8. Comandos Operacionais

### Standalone (runner principal)
```bash
run_po_bot.bat
python src/po_intraday_trader.py
```

### Estratégia 3 standalone
```bash
python -m src.strategy3.strategy3_main
```

### TraderDev (desenvolvimento local)
```bash
cd TraderDev
docker compose up
# API: http://localhost:8000
# Frontend: http://localhost:3000
```

### Produção (VPS)
```bash
# No VPS via SSH:
docker compose -f docker-compose.yml -f docker-compose.traefik.yml pull
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d
# UI: https://pocketoption.tradixio.com
```

---

## 9. Estratégias Futuras (Fase 4)

A serem implementadas após a migração base:

| Estratégia | Tipo | Expiração | Win Rate Esperado |
|-----------|------|-----------|------------------|
| S4 Pinbar Reversal | M5 reversão | 5 min | 65-72% |
| S5 Engulfing Pattern | M1 scalp | 1 min | 60-65% |
| S6 OTC Weekend | M5 fins de semana | 5 min | 58-65% |
| S7 Divergência RSI/MACD | M5 | 5 min | 62-68% |

---

## 10. Referências e Histórico

- **Origem:** Migrado do AlpacaTrader (paper trading $100k BTC/USD)
- **Conta demo PocketOption:** UID 2197423
- **Conta real:** Disponível (POCKET_DEMO=0)
- **Plano detalhado:** `PLAN.md`
- **Repositório GitHub:** https://github.com/romualdoalves/pocketoptiontrader.git
