"""
live_agent.py — Deploy the trained FinRL PPO agent to live Alpaca paper trading.

Replaces the Claude prompt scheduler for deep RL mode (Approach C, Level 4).
Runs on a schedule (e.g. every 5 minutes during market hours) to fetch live
state, predict actions, and submit orders to Alpaca.

Schedule via cron (every 5 min, Mon-Fri, 9:30 AM - 4:00 PM ET):
    */5 9-15 * * 1-5 python3 ~/trading-bot/src/live_agent.py >> ~/trading-bot/logs/live_agent.log 2>&1

Prerequisites:
    pip install stable-baselines3 alpaca-py pandas numpy ta

Environment variables required (loaded from .env):
    ALPACA_API_KEY
    ALPACA_API_SECRET
    ALPACA_BASE_URL   (default: https://paper-api.alpaca.markets)
"""

import os
import sys
import json
import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

ROOT       = os.path.join(os.path.dirname(__file__), "..")
MODEL_PATH = os.path.join(ROOT, "models", "ppo_trading_agent")

TICKER_LIST  = ["TSLA", "AAPL", "NVDA", "SPY"]
MAX_SHARES   = 10   # hard cap per ticker per order
LOOKBACK     = 20   # bars of history needed for indicators


def load_env():
    """Load credentials from .env file if present, otherwise use os.environ."""
    env_path = os.path.join(ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def get_alpaca_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_API_SECRET"],
        paper=True,
    )


def get_data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_API_SECRET"],
    )


def fetch_bars(data_client, tickers: list[str], limit: int = LOOKBACK + 5) -> dict:
    """Fetch recent daily bars for each ticker. Returns dict of {ticker: [bars]}."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    request = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame.Day,
        limit=limit,
    )
    bars = data_client.get_stock_bars(request)
    return {ticker: bars[ticker] for ticker in tickers if ticker in bars}


def compute_indicators(bars: list) -> dict:
    """
    Compute a minimal set of technical indicators from a bar series.
    Returns a flat dict of indicator values (latest bar only).
    """
    closes = np.array([b.close for b in bars], dtype=float)

    if len(closes) < 14:
        return {}

    # RSI (14)
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[-14:])
    avg_loss = np.mean(losses[-14:]) + 1e-9
    rs  = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    # MACD (12, 26 EMA diff) — simplified using available bars
    ema12 = float(np.mean(closes[-12:])) if len(closes) >= 12 else float(closes[-1])
    ema26 = float(np.mean(closes[-26:])) if len(closes) >= 26 else float(closes[-1])
    macd  = ema12 - ema26

    return {
        "close":  float(closes[-1]),
        "rsi":    float(rsi),
        "macd":   float(macd),
    }


def build_state(trading_client, data_client) -> np.ndarray:
    """
    Build the observation vector that matches the FinRL StockTradingEnv state space:
      [cash_balance, price_1..N, indicator_1_1..indicator_K_N]
    """
    account = trading_client.get_account()
    cash    = float(account.cash)

    bars_by_ticker = fetch_bars(data_client, TICKER_LIST)

    prices     = []
    indicators = []

    for ticker in TICKER_LIST:
        if ticker not in bars_by_ticker or len(bars_by_ticker[ticker]) < 14:
            prices.append(0.0)
            indicators.extend([50.0, 0.0])  # neutral defaults
            continue

        ind = compute_indicators(bars_by_ticker[ticker])
        prices.append(ind.get("close", 0.0))
        indicators.extend([ind.get("rsi", 50.0), ind.get("macd", 0.0)])

    state = np.array([cash] + prices + indicators, dtype=np.float32)
    return state


def execute_action(trading_client, action: np.ndarray) -> None:
    """
    Translate the model's continuous action vector into Alpaca orders.

    action shape: (N,) where N = len(TICKER_LIST)
    Convention (matching FinRL StockTradingEnv):
      action[i] > 0  → buy  int(action[i] * MAX_SHARES) shares
      action[i] < 0  → sell int(-action[i] * MAX_SHARES) shares
      action[i] ≈ 0  → hold
    """
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    for i, ticker in enumerate(TICKER_LIST):
        raw = float(action[i])
        qty = int(abs(raw) * MAX_SHARES)

        if qty == 0:
            continue

        side = OrderSide.BUY if raw > 0 else OrderSide.SELL

        try:
            order_req = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            order = trading_client.submit_order(order_req)
            ts = datetime.datetime.utcnow().isoformat()
            print(f"[{ts}Z] {side.value} {qty} {ticker} — order id: {order.id}")
        except Exception as e:
            print(f"[ERROR] Failed to place {side.value} {qty} {ticker}: {e}")


def is_market_open(trading_client) -> bool:
    clock = trading_client.get_clock()
    return clock.is_open


def main():
    load_env()

    try:
        from stable_baselines3 import PPO
    except ImportError:
        print("stable-baselines3 not installed. Run: pip install stable-baselines3")
        sys.exit(1)

    if not os.path.exists(MODEL_PATH + ".zip"):
        print(f"No trained model at {MODEL_PATH}.zip — run train_agent.py first.")
        sys.exit(1)

    trading_client = get_alpaca_client()
    data_client    = get_data_client()

    if not is_market_open(trading_client):
        print(f"[{datetime.datetime.utcnow().isoformat()}Z] Market closed — skipping.")
        return

    print(f"[{datetime.datetime.utcnow().isoformat()}Z] Market open — running agent step.")

    model = PPO.load(MODEL_PATH)
    obs   = build_state(trading_client, data_client)

    action, _ = model.predict(obs, deterministic=True)
    execute_action(trading_client, action)

    print(f"[{datetime.datetime.utcnow().isoformat()}Z] Step complete.\n")


if __name__ == "__main__":
    main()
