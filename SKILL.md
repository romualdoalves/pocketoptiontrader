---
name: pocketoptiontrader
description: |
  PocketOptionTrader - Bot de trading de opções binárias na plataforma PocketOption.

  Migração completa do AlpacaTrader (spot crypto) para PocketOption (opções binárias).
  Todas as estratégias originais do Alpaca são mantidas com a camada de execução
  substituída por um conector WebSocket PocketOption.

  Estratégias ativas:
    - Estratégia 2 "Liquidity Sentinel" (BTCUSD, EURUSD - 5min expiry) — adaptada para binário
    - Estratégia 3 "AI Allocator" (multi-asset, LSTM alpha, Kelly sizing) — adaptada para binário
    - 25+ estratégias modulares no TraderDev backend

  Implementações:
    1. STANDALONE  (src/po_intraday_trader.py + scheduler.py) — runner principal
    2. TRADERDEV   (TraderDev/ — FastAPI + React + PostgreSQL) — UI web completa

  Deploy:
    - Local: Windows (scheduler + .bat scripts)
    - Produção: pocketoption.tradixio.com (VPS Hostinger KVM4, Docker + Traefik)

  Use este skill quando perguntar sobre: bot PocketOption, Estratégia 2/3, Liquidity
  Sentinel, Defensive Allocator, LSTM, Volume Profile, Engine A/B, Kelly criterion,
  win rate circuit breaker, conector WebSocket, deploy VPS, Traefik.
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
├── scheduler.py                       # Runner 5-min (PocketOption)
├── run_po_bot.bat                      # Lançamento Windows
├── docker-compose.yml                 # Stack completo
├── docker-compose.traefik.yml         # Overlay produção (VPS + Traefik)
├── .github/workflows/ci.yml           # CI básico
│
├── src/
│   ├── pocket_option/                 # Camada core PocketOption
│   │   ├── connector.py               # WebSocket auth + lifecycle
│   │   ├── data_feed.py               # Candles OHLCV em tempo real
│   │   └── trade_manager.py           # place_trade(), get_result()
│   │
│   ├── po_intraday_trader.py          # Estratégia 2 binária (ATIVO)
│   ├── intraday_trader.py             # Estratégia 2 original Alpaca (REFERÊNCIA)
│   ├── trade_logger.py                # Persistência JSONL
│   ├── reward_function.py             # Recompensa win/loss binário
│   ├── regime_detector.py             # Detecção de regime
│   ├── evolve.py                      # Level 4A — Evolution Strategies
│   ├── bayesian_optimize.py           # Level 4B — Optuna
│   ├── meta_optimizer.py              # Level 4D — Claude API meta-review
│   └── strategy3/
│       ├── po_executor.py             # Execução binária multi-asset
│       ├── strategy3_main.py          # Orquestrador (usa po_executor)
│       ├── lstm_alpha.py              # LSTM PyTorch (sinais inalterados)
│       ├── train_lstm.py              # Treino do LSTM
│       ├── allocator.py               # Kelly criterion por ativo
│       ├── risk_manager.py            # Drawdown tiers + win rate breaker
│       ├── macro_filter.py            # Binance funding rate + OI
│       ├── universe.py                # Pares PocketOption
│       ├── indicators.py              # EMA, RSI, MACD, VWAP, ATR
│       └── state.py                   # Estado: win/loss streaks, equity
│
├── config/
│   ├── pocket_option_params.json      # Config opções binárias
│   ├── strategy_params.json
│   └── population.json
│
├── logs/
│   ├── po_trade_log.jsonl             # Log de apostas binárias
│   └── scheduler.log
│
└── TraderDev/
    └── backend/
        ├── exchanges/
        │   ├── base.py
        │   ├── pocket_option.py       # Adapter PocketOption
        │   ├── alpaca.py              # Mantido (referência)
        │   └── factory.py
        └── strategies/               # 25+ estratégias (inalteradas)
```

---

## 4. Estratégia 2: Liquidity Sentinel → Binário (5 fases)

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

### Deploy rápido no VPS
```bash
git clone https://github.com/romualdoalves/pocketoptiontrader.git
cd pocketoptiontrader
cp .env.example .env
# editar .env com credenciais PocketOption
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d
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
