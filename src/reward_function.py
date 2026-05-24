"""
reward_function.py — Composite reward scorer for evaluating strategy performance.

Used by evolve.py (Evolution Strategies) and bayesian_optimize.py to score
a set of closed trades. Higher reward = better risk-adjusted performance.

Reward formula (weighted composite):
  - 50%: Sharpe ratio (mean return / std deviation)
  - 30%: Win rate (fraction of trades with positive P&L)
  - 20%: Drawdown avoidance (penalizes large cumulative loss streaks)

Minimum of 5 closed trades required to produce a score. Returns 0.0 otherwise.
"""

import numpy as np
from trade_logger import load_closed_trades


def compute_reward(trade_log: list[dict]) -> float:
    """
    Score a list of closed trade dicts on risk-adjusted performance.

    Args:
        trade_log: List of merged entry+outcome dicts (from load_closed_trades).
                   Each must have a "pnl_pct" field.

    Returns:
        Composite reward scalar. Higher is better. Returns 0.0 if < 5 trades.
    """
    returns = [t["pnl_pct"] for t in trade_log if "pnl_pct" in t]

    if len(returns) < 5:
        return 0.0

    returns_arr = np.array(returns)

    mean_r = np.mean(returns_arr)
    std_r = np.std(returns_arr) + 1e-9  # avoid division by zero
    sharpe = mean_r / std_r

    win_rate = float(np.sum(returns_arr > 0)) / len(returns_arr)

    cumulative = np.cumsum(returns_arr)
    max_dd = float(np.min(cumulative))  # most negative cumulative sum = worst streak

    reward = (
        sharpe * 0.5
        + win_rate * 0.3
        + (1.0 + max_dd) * 0.2  # 1 + max_dd: near 1.0 if no drawdown, <1 if losses
    )
    return float(reward)


def compute_strategy_metrics(strategy: str = None) -> dict:
    """
    Load closed trades (optionally filtered by strategy) and return a full
    metrics breakdown suitable for reporting or the meta-optimizer prompt.

    Returns:
        dict with keys: win_rate, avg_pnl_pct, max_drawdown, sharpe, trade_count, reward
    """
    trades = load_closed_trades(strategy=strategy)
    returns = [t["pnl_pct"] for t in trades if "pnl_pct" in t]

    if len(returns) < 2:
        return {
            "win_rate":      None,
            "avg_pnl_pct":   None,
            "max_drawdown":  None,
            "sharpe":        None,
            "trade_count":   len(returns),
            "reward":        0.0,
        }

    returns_arr = np.array(returns)
    cumulative = np.cumsum(returns_arr)

    mean_r = float(np.mean(returns_arr))
    std_r = float(np.std(returns_arr)) + 1e-9
    sharpe = mean_r / std_r
    win_rate = float(np.sum(returns_arr > 0)) / len(returns_arr)
    max_dd = float(np.min(cumulative))

    return {
        "win_rate":     round(win_rate, 4),
        "avg_pnl_pct":  round(mean_r, 4),
        "max_drawdown": round(max_dd, 4),
        "sharpe":       round(sharpe, 4),
        "trade_count":  len(returns),
        "reward":       round(compute_reward(trades), 4),
    }


if __name__ == "__main__":
    import json

    print("=== Overall Performance ===")
    print(json.dumps(compute_strategy_metrics(), indent=2))

    for strat in ["trailing_stop", "ladder", "wheel", "copy_trading"]:
        metrics = compute_strategy_metrics(strategy=strat)
        if metrics["trade_count"]:
            print(f"\n=== {strat} ===")
            print(json.dumps(metrics, indent=2))
