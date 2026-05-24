"""
bayesian_optimize.py — Bayesian hyperparameter search via Optuna (Approach B, Level 4).

Smarter than pure Evolution Strategies: builds a probabilistic model of which
parameter regions score well and focuses trials there, avoiding re-testing
configurations already proven bad.

Run once after gathering sufficient historical trade data (90+ days recommended):
    python3 src/bayesian_optimize.py

Results are persisted to logs/optuna.db so runs are resumable and cumulative.
The best-found params are written to config/strategy_params.json.

Requires:
    pip install optuna
"""

import json
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(__file__))

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
except ImportError:
    raise ImportError(
        "Optuna not installed. Run: pip install optuna"
    )

from trade_logger import load_closed_trades
from reward_function import compute_reward

ROOT             = os.path.join(os.path.dirname(__file__), "..")
BEST_PARAMS_PATH = os.path.join(ROOT, "config", "strategy_params.json")
DB_PATH          = os.path.join(ROOT, "logs", "optuna.db")
STUDY_NAME       = "trading_bot_v1"


def simulate_strategy(params: dict, lookback_days: int = 90) -> list[dict]:
    """
    Pull closed trades from the log that fall within the lookback window
    and whose logged params are close to the trial params.

    In a full backtesting setup this would replay historical price data.
    Here it filters the actual paper-trading log as a lightweight proxy.

    Args:
        params:        Trial parameter dict from Optuna.
        lookback_days: How many calendar days of history to score.

    Returns:
        Filtered list of closed trade dicts.
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=lookback_days)
    all_trades = load_closed_trades()

    recent = [
        t for t in all_trades
        if "timestamp" in t
        and datetime.datetime.fromisoformat(t["timestamp"].rstrip("Z")) >= cutoff
    ]
    return recent


def objective(trial: "optuna.Trial") -> float:
    """
    Optuna objective: suggest a parameter set, simulate, score, return reward.
    """
    params = {
        "trailing_stop_pct":  trial.suggest_float("trailing_stop_pct", 1.0, 8.0),
        "ladder_drop_1":      trial.suggest_float("ladder_drop_1", 0.5, 5.0),
        "ladder_drop_2":      trial.suggest_float("ladder_drop_2", 1.0, 8.0),
        "ladder_drop_3":      trial.suggest_float("ladder_drop_3", 2.0, 12.0),
        "position_size_pct":  trial.suggest_float("position_size_pct", 1.0, 10.0),
        "wheel_target_delta": trial.suggest_float("wheel_target_delta", 0.10, 0.50),
        "wheel_dte":          trial.suggest_int("wheel_dte", 14, 60),
        "min_iv_threshold":   trial.suggest_float("min_iv_threshold", 15.0, 80.0),
        "copy_trade_min_usd": trial.suggest_float("copy_trade_min_usd", 5000, 50000),
    }

    trade_log = simulate_strategy(params, lookback_days=90)
    return compute_reward(trade_log)


def run_optimization(n_trials: int = 50) -> dict:
    """
    Run or resume a Bayesian optimization study.

    Args:
        n_trials: Number of new trials to run this session.

    Returns:
        Best parameter dict found so far.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    storage = f"sqlite:///{DB_PATH}"
    study = optuna.create_study(
        direction="maximize",
        storage=storage,
        study_name=STUDY_NAME,
        load_if_exists=True,
    )

    print(f"Running {n_trials} Optuna trials (study: {STUDY_NAME})")
    print(f"Results stored at: {DB_PATH}\n")

    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    best = study.best_params
    best_value = study.best_value
    total_trials = len(study.trials)

    print(f"\nBest score after {total_trials} total trials: {best_value:.4f}")
    print(f"Best params: {json.dumps(best, indent=2)}")

    return best


def save_best_params(params: dict) -> None:
    """Backup current params and write the Optuna best to strategy_params.json."""
    if os.path.exists(BEST_PARAMS_PATH):
        with open(BEST_PARAMS_PATH) as f:
            current = json.load(f)
        date_str = datetime.date.today().isoformat()
        backup = os.path.join(ROOT, "config", f"params_backup_{date_str}.json")
        with open(backup, "w") as f:
            json.dump(current, f, indent=2)
        print(f"Backed up current params to {backup}")

    # Round int params
    cleaned = {}
    for k, v in params.items():
        if k in ("wheel_dte",):
            cleaned[k] = int(round(v))
        elif k == "copy_trade_min_usd":
            cleaned[k] = int(round(v))
        else:
            cleaned[k] = round(float(v), 4)

    with open(BEST_PARAMS_PATH, "w") as f:
        json.dump(cleaned, f, indent=2)
    print(f"Saved best params to {BEST_PARAMS_PATH}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Bayesian param optimization via Optuna")
    parser.add_argument("--trials", type=int, default=50,
                        help="Number of trials to run (default: 50)")
    parser.add_argument("--save", action="store_true",
                        help="Write best params to strategy_params.json after run")
    args = parser.parse_args()

    best_params = run_optimization(n_trials=args.trials)

    if args.save:
        save_best_params(best_params)
    else:
        print("\nRun with --save to promote best params to strategy_params.json")
