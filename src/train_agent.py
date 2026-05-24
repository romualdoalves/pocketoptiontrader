"""
train_agent.py — Deep RL agent training via FinRL + Stable-Baselines3 (Approach C, Level 4).

This is the most powerful but also most complex approach. It trains a PPO (Proximal
Policy Optimization) neural network on 2+ years of historical Alpaca market data.
The trained model is saved to models/ppo_trading_agent.zip and loaded by live_agent.py.

Prerequisites:
    pip install finrl stable-baselines3 gymnasium alpaca-py pandas numpy

Usage:
    python3 src/train_agent.py --train          # train on 2022-2023 data
    python3 src/train_agent.py --validate       # evaluate on 2024 holdout data
    python3 src/train_agent.py --train --validate  # train then validate

Notes:
- Training 500k timesteps takes ~10-30 minutes on CPU, ~2-5 min on GPU.
- Keep the validate Sharpe ratio above 0.5 before going live.
- Retrain monthly to avoid model drift as market regimes change.
"""

import os
import sys
import argparse
import json
import datetime

ROOT       = os.path.join(os.path.dirname(__file__), "..")
MODEL_DIR  = os.path.join(ROOT, "models")
MODEL_PATH = os.path.join(MODEL_DIR, "ppo_trading_agent")

TICKER_LIST  = ["TSLA", "AAPL", "NVDA", "SPY"]
TRAIN_START  = "2022-01-01"
TRAIN_END    = "2023-12-31"
VAL_START    = "2024-01-01"
VAL_END      = "2024-12-31"
INITIAL_CASH = 100_000
TIMESTEPS    = 500_000


def _check_imports():
    missing = []
    try:
        import finrl  # noqa: F401
    except ImportError:
        missing.append("finrl")
    try:
        import stable_baselines3  # noqa: F401
    except ImportError:
        missing.append("stable-baselines3")
    try:
        import gymnasium  # noqa: F401
    except ImportError:
        missing.append("gymnasium")
    if missing:
        raise ImportError(
            f"Missing packages: {missing}\n"
            f"Install with: pip install {' '.join(missing)}"
        )


def download_and_prepare_data(start: str, end: str):
    """Download historical OHLCV + technical indicators from Alpaca via FinRL."""
    from finrl.config import INDICATORS
    from finrl.meta.data_processor import DataProcessor

    dp = DataProcessor(
        data_source="alpaca",
        start_date=start,
        end_date=end,
        time_interval="1D",
    )
    df = dp.download_data(ticker_list=TICKER_LIST)
    df = dp.clean_data(df)
    df = dp.add_technical_indicator(df, INDICATORS)
    return df, INDICATORS


def build_env(df, indicators: list):
    """Construct a FinRL StockTradingEnv from a prepared DataFrame."""
    from finrl.meta.env_stock_trading.env_stocktrading import StockTradingEnv

    stock_dim  = len(TICKER_LIST)
    state_space = len(indicators) * stock_dim + stock_dim + 1  # indicators + prices + cash

    env = StockTradingEnv(
        df=df,
        stock_dim=stock_dim,
        hmax=100,
        initial_amount=INITIAL_CASH,
        transaction_cost_pct=0.001,
        reward_scaling=1e-4,
        state_space=state_space,
        action_space=stock_dim,
        tech_indicator_list=indicators,
    )
    return env


def train():
    """Train the PPO agent on the training date range and save the model."""
    from stable_baselines3 import PPO

    _check_imports()
    os.makedirs(MODEL_DIR, exist_ok=True)

    print(f"Downloading training data ({TRAIN_START} to {TRAIN_END})...")
    train_df, indicators = download_and_prepare_data(TRAIN_START, TRAIN_END)

    print("Building training environment...")
    train_env = build_env(train_df, indicators)

    print(f"Training PPO for {TIMESTEPS:,} timesteps...")
    model = PPO(
        "MlpPolicy",
        train_env,
        verbose=1,
        learning_rate=3e-4,
        n_steps=2048,
        batch_size=64,
        n_epochs=10,
        gamma=0.99,
        gae_lambda=0.95,
    )
    model.learn(total_timesteps=TIMESTEPS)
    model.save(MODEL_PATH)
    print(f"Model saved to {MODEL_PATH}.zip")

    # Save training metadata
    meta = {
        "trained_at":   datetime.datetime.utcnow().isoformat() + "Z",
        "train_start":  TRAIN_START,
        "train_end":    TRAIN_END,
        "tickers":      TICKER_LIST,
        "timesteps":    TIMESTEPS,
        "algorithm":    "PPO",
    }
    with open(os.path.join(MODEL_DIR, "training_meta.json"), "w") as f:
        json.dump(meta, f, indent=2)


def validate():
    """Evaluate the saved model on the validation date range."""
    from stable_baselines3 import PPO

    _check_imports()

    if not os.path.exists(MODEL_PATH + ".zip"):
        print(f"No model found at {MODEL_PATH}.zip — run with --train first.")
        return

    print(f"Downloading validation data ({VAL_START} to {VAL_END})...")
    val_df, indicators = download_and_prepare_data(VAL_START, VAL_END)

    print("Building validation environment...")
    val_env = build_env(val_df, indicators)

    print("Loading model...")
    model = PPO.load(MODEL_PATH)

    print("Running validation episode...")
    obs = val_env.reset()
    done = False
    total_reward = 0.0
    steps = 0

    while not done:
        action, _ = model.predict(obs, deterministic=True)
        obs, reward, done, info = val_env.step(action)
        total_reward += reward
        steps += 1

    final_portfolio = val_env.asset_memory[-1] if hasattr(val_env, "asset_memory") else "N/A"
    print(f"\nValidation complete ({steps} steps):")
    print(f"  Total reward:       {total_reward:.4f}")
    print(f"  Final portfolio:    ${final_portfolio:,.2f}" if isinstance(final_portfolio, (int, float)) else f"  Final portfolio:  {final_portfolio}")
    print(f"  Initial cash:       ${INITIAL_CASH:,.2f}")

    if hasattr(val_env, "sharpe_ratio"):
        print(f"  Sharpe ratio:       {val_env.sharpe_ratio():.4f}")

    if isinstance(final_portfolio, (int, float)):
        pnl_pct = (final_portfolio - INITIAL_CASH) / INITIAL_CASH * 100
        print(f"  P&L:                {pnl_pct:+.2f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train/validate FinRL PPO trading agent")
    parser.add_argument("--train",    action="store_true", help="Train the model")
    parser.add_argument("--validate", action="store_true", help="Validate the saved model")
    args = parser.parse_args()

    if not args.train and not args.validate:
        parser.print_help()
        sys.exit(0)

    if args.train:
        train()
    if args.validate:
        validate()
