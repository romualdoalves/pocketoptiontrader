# PocketOptionTrader — Plano Completo de Migração Alpaca → PocketOption

**Data do plano:** 2026-05-24  
**Repositório:** https://github.com/romualdoalves/pocketoptiontrader.git  
**Deploy:** pocketoption.tradixio.com (VPS Hostinger KVM4 — Docker + Traefik pré-instalados)

---

## 1. Visão Geral da Arquitetura

```
PocketOptionTrader/
├── .env                               # Credenciais PocketOption (nunca commitar)
├── .env.example                       # Template de credenciais
├── SKILL.md                           # Documentação completa do projeto
├── PLAN.md                            # Este arquivo
├── requirements.txt                   # Dependências Python atualizadas
├── scheduler.py                       # Runner 5-min adaptado para PocketOption
├── run_po_bot.bat                      # Script de lançamento Windows
├── docker-compose.yml                 # Stack completo (API + Frontend + PostgreSQL)
├── docker-compose.traefik.yml         # Overlay para produção com Traefik (VPS)
├── Dockerfile.api                     # FastAPI backend
├── .github/
│   └── workflows/
│       └── ci.yml                     # CI: lint + testes básicos
│
├── src/
│   ├── pocket_option/                 # NOVO: Camada core PocketOption
│   │   ├── __init__.py
│   │   ├── connector.py               # WebSocket auth + lifecycle de conexão
│   │   ├── data_feed.py               # Subscrição de candles OHLCV em tempo real
│   │   └── trade_manager.py           # Colocar call/put, monitorar resultado
│   │
│   ├── po_intraday_trader.py          # Estratégia 2 adaptada para opções binárias
│   ├── intraday_trader.py             # MANTIDO (referência Alpaca)
│   ├── trade_logger.py                # Inalterado (persistência JSONL)
│   ├── reward_function.py             # Adaptado: recompensa por win/loss binário
│   ├── regime_detector.py             # Inalterado
│   ├── evolve.py                      # Inalterado (Level 4A - Evolution Strategies)
│   ├── bayesian_optimize.py           # Inalterado (Optuna)
│   ├── meta_optimizer.py              # Inalterado (Level 4D - Claude API)
│   └── strategy3/
│       ├── po_executor.py             # NOVO: substitui alpaca_executor.py
│       ├── strategy3_main.py          # Adaptado: swap do executor
│       ├── lstm_alpha.py              # Inalterado (geração de sinais)
│       ├── allocator.py               # Adaptado: alocação de aposta (Kelly)
│       ├── risk_manager.py            # Adaptado: circuit breaker por win rate
│       ├── macro_filter.py            # Inalterado (Binance API público)
│       ├── universe.py                # Adaptado: pares PocketOption
│       ├── indicators.py              # Inalterado
│       └── state.py                   # Adaptado: rastrear streaks win/loss
│
├── config/
│   ├── pocket_option_params.json      # NOVO: configuração opções binárias
│   ├── strategy_params.json           # Atualizado para opções binárias
│   └── population.json               # Inalterado
│
├── prompts/                           # Inalterado
├── logs/                              # Inalterado
│
└── TraderDev/
    └── backend/
        ├── exchanges/
        │   ├── base.py                # Inalterado
        │   ├── pocket_option.py       # NOVO: adapter PocketOption
        │   ├── alpaca.py              # MANTIDO (compatibilidade)
        │   └── factory.py             # Atualizado: registrar "pocket_option"
        └── strategies/               # TODAS as 25+ estratégias mantidas
            └── ...                    # Signal generation inalterado
```

---

## 2. Diferenças Fundamentais: Alpaca Spot → PocketOption Binário

| Aspecto | Alpaca (Spot) | PocketOption (Binário) |
|---------|--------------|------------------------|
| Tipo de ordem | Market / Limit / Stop | Call (alta) / Put (baixa) |
| Gestão de posição | Entry + Stop Loss + Take Profit | Não existe — resultado na expiração |
| Sizing | Quantidade de ativos (Kelly/vol) | Valor em USD por aposta |
| Saída | Manual ou SL/TP | Automática na expiração |
| Resultado | P&L variável | Fixo: +80-95% (win) ou -100% (loss) |
| Break-even | Qualquer win rate positivo | >52.6% com 90% payout |
| Horário | Mercado aberto | 24/7 (OTC nos fins de semana) |
| Short | Não (Alpaca spot) | Sim (Put = aposta de queda) |

---

## 3. Fases de Implementação

### FASE 1 — Conector PocketOption (`src/pocket_option/`)

**3.1 connector.py**
```python
# Autenticação via cookies de sessão (SSID + UID)
# Biblioteca: pocketoptionapi (PyPI)
from pocketoptionapi.stable_api import PocketOption

api = PocketOption(ssid=POCKET_SSID, demo=bool(POCKET_DEMO))
await api.connect()
```

- Gerenciamento do ciclo de vida da conexão WebSocket
- Reconexão automática com backoff exponencial
- Alternância demo/real via env var `POCKET_DEMO`
- Health check periódico (ping/pong)

**3.2 data_feed.py**
- Subscrição de candles por par e timeframe (1m, 5m, 15m)
- Buffer circular de 100 candles por ativo
- Callback assíncrono para novas barras
- Sincronização inicial (backfill dos últimos 60 candles)
- Pares suportados: EURUSD, GBPUSD, USDJPY, BTCUSD, ETHUSD, XAUUSD, AAPL_OTC, EURUSD_OTC

**3.3 trade_manager.py**
```python
async def place_trade(asset, amount, direction, expiry_seconds):
    # direction: "call" (alta) ou "put" (baixa)
    # expiry_seconds: 60, 300 (5min), 900 (15min)
    # retorna: trade_id, open_price
    
async def get_result(trade_id):
    # retorna: "win", "loss", "draw" + close_price + profit
    
async def get_balance():
    # retorna: saldo atual (demo ou real)
```

---

### FASE 2 — Adaptação da Estratégia 2: Liquidity Sentinel → Binário

**Arquivo:** `src/po_intraday_trader.py`

A lógica de geração de sinais é **100% idêntica** ao `intraday_trader.py` original:
- Fase 1: Macro Filter (Binance funding rate + OI)
- Fase 2: Volume Profile (VAH/POC/VAL)
- Fase 3: Engine A (breakout IMBALANCED) + Engine B (mean reversion BALANCED)
- Fase 4: Indicadores (EMA50, VWAP, ATR14, MFI14, Delta Z-Score)

**Mudanças exclusivas na camada de execução:**

| Original (Alpaca) | Novo (PocketOption) |
|---|---|
| `trading.submit_order(side="buy")` | `trade_manager.place_trade(direction="call")` |
| `trading.submit_order(side="sell")` | `trade_manager.place_trade(direction="put")` |
| Stop loss = POC - 2 ticks | Não existe → aposta expira em 5 min |
| Sizing = BUY_AMOUNT × (1/vol_index) | Bet = kelly_fraction × account_balance |
| Engine A short = desabilitado | Engine A short = PUT habilitado |
| Circuit breaker: -$3,000 diário | Circuit breaker: win_rate < 50% / 20 trades |

**Kelly Criterion para bet sizing:**
```
f* = (win_rate × payout - (1 - win_rate)) / payout
bet = f* × account_balance
# Limitado a máx 5% do saldo por aposta
```

**Novidade com PocketOption:** Engine A agora pode operar SHORT (PUT) quando:
- Preço < VAL E prev_close >= VAL (breakdown)  
- delta_z <= -2.0 (pressão de venda)  
- preço < VWAP E < EMA50  
- macro OK (sem funding rate negativo extremo)

---

### FASE 3 — Adaptação da Estratégia 3: Alocador Multi-Asset → Binário

**Arquivo:** `src/strategy3/po_executor.py` (substitui `alpaca_executor.py`)

```python
async def execute_signals(signals: dict[str, str], risk_params: dict):
    # signals: {"BTCUSD": "LONG", "ETHUSD": "SHORT", "EURUSD": "HOLD"}
    # Para cada LONG → place_trade("call", kelly_bet, expiry=300)
    # Para cada SHORT → place_trade("put", kelly_bet, expiry=300)
    # Para HOLD → pula
```

**Adaptações no risk_manager.py:**
- Tier de drawdown mantido (0-2%, 2-4%, 4-6%, 6%+)
- NOVO: Win Rate Circuit Breaker
  - Janela: últimas 20 apostas
  - Haltar se win_rate < 50% (break-even com 90% payout = 52.6%)
  - Cooldown: 60 min após ativação

**Adaptações no state.py:**
- Novo campo: `win_streak`, `loss_streak`, `rolling_win_rate_20`
- Novo campo: `consecutive_losses` → cooldown após 3 seguidas

**universe.py — Pares PocketOption (substituição do universo Alpaca):**
```python
CORE_ASSETS = [
    "EURUSD",   # Forex — maior liquidez
    "GBPUSD",   # Forex
    "USDJPY",   # Forex
    "BTCUSD",   # Cripto
    "ETHUSD",   # Cripto
    "XAUUSD",   # Ouro (hedge)
]
OTC_ASSETS = [  # Apenas fins de semana
    "EURUSD_OTC",
    "GBPUSD_OTC", 
    "USDJPY_OTC",
]
```

---

### FASE 4 — Novas Estratégias Específicas PocketOption

Serão implementadas **após** a migração base:

**Estratégia 4: Pinbar Reversal (M5)**
- Detecta pin bars (martelo/estrela cadente) em M5
- Confirmação: RSI + volume acima da média
- Expiração: 5 minutos
- Win rate histórico esperado: 65-72%

**Estratégia 5: Engulfing Pattern (M1)**
- Candles de engolfo bearish/bullish com volume
- Expiração: 1 minuto (scalp agressivo)
- Filtro: hora de alta volatilidade (abertura Londres/NY)

**Estratégia 6: OTC Weekend (M5)**
- Opera apenas sábado e domingo nos pares _OTC
- Adapta os indicadores para o spread maior do OTC
- Usar Engine B (mean reversion) prioritariamente

**Estratégia 7: Divergência RSI/MACD (M5)**
- Divergência bullish/bearish entre preço e RSI ou MACD
- Maior probabilidade em extremos: RSI < 30 ou > 70
- Expiração: 5 minutos

---

### FASE 5 — TraderDev UI: Suporte a PocketOption

**Novo arquivo:** `TraderDev/backend/exchanges/pocket_option.py`

Implementa a interface `BaseExchange`:
```python
class PocketOptionExchange(BaseExchange):
    async def fetch_candles(asset, timeframe, limit) → pd.DataFrame
    async def get_ticker(asset) → dict
    async def get_balance() → float
    async def get_position(asset) → dict   # retorna última aposta em aberto
    async def market_order(asset, side, amount) → dict  # coloca call/put
```

**Atualização do factory.py:**
```python
"pocket_option": PocketOptionExchange
```

**Novas métricas no frontend:**
- Win Rate (%) por estratégia e total
- Payout médio recebido
- Sequência atual (win/loss streak)
- Break-even necessário vs win rate atual
- Gráfico de saldo ao longo do tempo

---

### FASE 6 — Deploy no VPS Hostinger (pocketoption.tradixio.com)

**Infraestrutura existente:**
- VPS KVM4 Hostinger
- Docker + Traefik pré-instalados
- Subdomínio: `pocketoption.tradixio.com`

**docker-compose.traefik.yml** (overlay de produção):
```yaml
services:
  api:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.potrader.rule=Host(`pocketoption.tradixio.com`)"
      - "traefik.http.routers.potrader.entrypoints=websecure"
      - "traefik.http.routers.potrader.tls.certresolver=letsencrypt"
      - "traefik.http.services.potrader.loadbalancer.server.port=8000"
  
  frontend:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.potrader-ui.rule=Host(`pocketoption.tradixio.com`) && PathPrefix(`/`)"
      - "traefik.http.routers.potrader-ui.entrypoints=websecure"
      - "traefik.http.routers.potrader-ui.tls.certresolver=letsencrypt"
```

**Comandos de deploy no VPS:**
```bash
git clone https://github.com/romualdoalves/pocketoptiontrader.git
cd pocketoptiontrader
cp .env.example .env
# editar .env com credenciais PocketOption
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d
```

**CI/CD via GitHub Actions** (`.github/workflows/ci.yml`):
- Trigger: push para `main`
- Steps: lint (ruff) + testes unitários
- Futuro: deploy automático no VPS via SSH

---

## 4. Tabela de Arquivos — Ações

| Arquivo | Ação | Fase |
|---------|------|------|
| `.env.example` | ATUALIZAR — vars PocketOption | 1 |
| `requirements.txt` | ATUALIZAR — add pocketoptionapi | 1 |
| `src/pocket_option/__init__.py` | CRIAR | 1 |
| `src/pocket_option/connector.py` | CRIAR — WebSocket auth | 1 |
| `src/pocket_option/data_feed.py` | CRIAR — candles em tempo real | 1 |
| `src/pocket_option/trade_manager.py` | CRIAR — place_trade, get_result | 1 |
| `src/po_intraday_trader.py` | CRIAR — Estratégia 2 para binário | 2 |
| `src/strategy3/po_executor.py` | CRIAR — substitui alpaca_executor | 3 |
| `src/strategy3/strategy3_main.py` | ATUALIZAR — swap executor | 3 |
| `src/strategy3/risk_manager.py` | ATUALIZAR — win rate circuit breaker | 3 |
| `src/strategy3/universe.py` | ATUALIZAR — pares PocketOption | 3 |
| `src/strategy3/state.py` | ATUALIZAR — win/loss streak tracking | 3 |
| `src/strategy3/allocator.py` | ATUALIZAR — Kelly criterion | 3 |
| `TraderDev/backend/exchanges/pocket_option.py` | CRIAR — BaseExchange adapter | 5 |
| `TraderDev/backend/exchanges/factory.py` | ATUALIZAR — registrar PO | 5 |
| `config/pocket_option_params.json` | CRIAR — config binário | 1 |
| `docker-compose.traefik.yml` | CRIAR — overlay Traefik VPS | 6 |
| `.github/workflows/ci.yml` | CRIAR — CI básico | 6 |
| `SKILL.md` | ATUALIZAR — documentação PocketOption | 1 |
| `scheduler.py` | ATUALIZAR — usar PO connector | 2 |
| `run_po_bot.bat` | CRIAR — script Windows | 2 |
| `src/intraday_trader.py` | MANTER — referência Alpaca | — |
| `TraderDev/backend/exchanges/alpaca.py` | MANTER — compatibilidade | — |
| `src/strategy3/alpaca_executor.py` | MANTER — referência | — |
| Todas as 25+ estratégias TraderDev | MANTER INALTERADAS | — |

---

## 5. Gerenciamento de Risco — Opções Binárias

### 5.1 Sizing por Kelly Criterion
```
Payout PocketOption: 80-95% (usar 85% como conservador)
Break-even win rate: 1 / (1 + 0.85) = 54.05%

Se win_rate estimada = 60%:
f* = (0.60 × 0.85 - 0.40) / 0.85 = (0.51 - 0.40) / 0.85 = 12.9%
Apostar 12.9% do saldo (usar ½ Kelly = 6.5% por segurança)
```

### 5.2 Limites Diários
- Máximo de apostas por dia: 20
- Máximo de perda diária: 15% do saldo
- Máximo por aposta: 5% do saldo
- Cooldown: 30 min após 3 losses consecutivos

### 5.3 Circuit Breakers
1. Win rate < 50% nas últimas 20 apostas → parar, aguardar 1h
2. Loss de 3 seguidas → parar, aguardar 30 min
3. Perda diária > 15% → parar, aguardar até próximo dia
4. Conexão perdida → reconectar, não apostar até sincronizar

---

## 6. Dependências

### Atualizar requirements.txt
```
# ADICIONAR
pocketoptionapi>=1.0.0         # Conector WebSocket PocketOption
websocket-client>=1.6.0         # Dependência do pocketoptionapi

# REMOVER
alpaca-py                        # Substituído

# MANTER
anthropic>=0.40.0               # Claude API (meta-optimizer)
pandas>=2.0.0
numpy>=1.24.0
optuna>=3.5.0
torch                           # LSTM Strategy 3
```

---

## 7. Configuração `.env`

```bash
# PocketOption
POCKET_SSID=c4c6843b30843f45982572f368941c91
POCKET_UID=2197423
POCKET_SECRET=b0f01b9e01a9a381be6a51a0ec8ddb83
POCKET_DEMO=1   # 1=demo, 0=real

# Anthropic (meta-optimizer)
ANTHROPIC_API_KEY=...

# Database (TraderDev)
DATABASE_URL=postgresql://crypto:crypto@localhost:5432/pocketoption
```

---

## 8. Ordem de Execução da Implementação

1. [ ] Atualizar `.env.example` e `requirements.txt`
2. [ ] Criar `src/pocket_option/connector.py`
3. [ ] Criar `src/pocket_option/data_feed.py`
4. [ ] Criar `src/pocket_option/trade_manager.py`
5. [ ] Criar `config/pocket_option_params.json`
6. [ ] Criar `src/po_intraday_trader.py` (Estratégia 2 binária)
7. [ ] Atualizar `scheduler.py`
8. [ ] Criar `src/strategy3/po_executor.py`
9. [ ] Atualizar `src/strategy3/risk_manager.py` + `universe.py` + `state.py` + `allocator.py`
10. [ ] Criar `TraderDev/backend/exchanges/pocket_option.py`
11. [ ] Atualizar `TraderDev/backend/exchanges/factory.py`
12. [ ] Criar `docker-compose.traefik.yml`
13. [ ] Criar `.github/workflows/ci.yml`
14. [ ] Atualizar `SKILL.md`
15. [ ] Push para GitHub `romualdoalves/pocketoptiontrader`

---

## 9. Perguntas em Aberto

Antes de iniciar a implementação, confirme:

1. **Pares prioritários:** Começar com EURUSD e BTCUSD, ou outro par?
2. **Expiração padrão:** 5 minutos (alinhado ao timeframe atual) ou outra?
3. **Aposta inicial (demo):** $10 por trade ou outro valor?
4. **Estratégias novas (Fase 4):** Implementar junto com a migração base ou depois?
5. **Conta real vs demo:** Ficar em demo até validar X trades ou há prazo?
