"""
evolve.py — Evolution Strategies weekly runner (Approach A, Level 4).

Runs a population of strategy parameter variants on paper trading,
scores them using the composite reward function, and evolves the next
generation by selecting top performers and mutating their parameters.

Schedule via cron (every Sunday at 6 PM):
    0 18 * * 0 python3 ~/trading-bot/src/evolve.py >> ~/trading-bot/logs/evolution.log 2>&1

The best-performing variant's parameters are promoted to:
    config/strategy_params.json  ← used by live bot prompts
"""

import json
import copy
import random
import os
import sys
import datetime

# Allow imports from this src/ directory
sys.path.insert(0, os.path.dirname(__file__))

from trade_logger import load_closed_trades
from reward_function import compute_reward

ROOT = os.path.join(os.path.dirname(__file__), "..")
POPULATION_PATH   = os.path.join(ROOT, "config", "population.json")
BEST_PARAMS_PATH  = os.path.join(ROOT, "config", "strategy_params.json")
BACKUP_DIR        = os.path.join(ROOT, "config")
LOG_DIR           = os.path.join(ROOT, "logs")

# Bounds for each parameter — mutations stay within these ranges
PARAM_BOUNDS = {
    "trailing_stop_pct":   (1.0,   8.0),
    "ladder_drop_1":       (0.5,   5.0),
    "ladder_drop_2":       (1.0,   8.0),
    "ladder_drop_3":       (2.0,  12.0),
    "position_size_pct":   (1.0,  10.0),
    "wheel_target_delta":  (0.10,  0.50),
    "wheel_dte":           (14,    60),
    "min_iv_threshold":    (15.0,  80.0),
    "copy_trade_min_usd":  (5000, 50000),
}


def load_population() -> list[dict]:
    with open(POPULATION_PATH) as f:
        return json.load(f)


def save_population(population: list[dict]) -> None:
    with open(POPULATION_PATH, "w") as f:
        json.dump(population, f, indent=2)


def score_variant(variant_index: int) -> float:
    """
    Score variant by loading its tagged trades from the trade log.

    Each trade logged by a variant should include a "variant_id" in the
    params snapshot. Falls back to all trades if no variant tagging found.
    """
    all_trades = load_closed_trades()
    variant_trades = [
        t for t in all_trades
        if t.get("params", {}).get("variant_id") == variant_index
    ]

    if not variant_trades:
        # No trades tagged to this variant yet — score 0
        return 0.0

    return compute_reward(variant_trades)


def mutate(params: dict, mutation_rate: float = 0.10) -> dict:
    """
    Create a mutated copy of params. Each float/int param is nudged by
    ±mutation_rate of its current value, then clamped to PARAM_BOUNDS.

    Args:
        params:        Parameter dict to mutate.
        mutation_rate: Fraction of current value by which to perturb (default 10%).

    Returns:
        New mutated parameter dict.
    """
    mutated = copy.deepcopy(params)
    mutated.pop("variant_id", None)  # strip old variant ID

    for key, val in mutated.items():
        if key not in PARAM_BOUNDS:
            continue

        lo, hi = PARAM_BOUNDS[key]
        delta = val * mutation_rate * random.uniform(-1.0, 1.0)
        new_val = val + delta

        if isinstance(val, int):
            new_val = int(round(new_val))
        else:
            new_val = round(new_val, 4)

        mutated[key] = max(lo, min(hi, new_val))

    return mutated


def evolve_population(
    population: list[dict],
    scores: list[float],
    pop_size: int = 10,
    top_k: int = 3,
) -> list[dict]:
    """
    Select the top_k highest-scoring variants, then fill the population
    back to pop_size by mutating those survivors.

    Args:
        population: Current list of parameter dicts.
        scores:     Parallel list of reward scores.
        pop_size:   Target population size.
        top_k:      Number of survivors to keep unchanged.

    Returns:
        New population list of size pop_size.
    """
    ranked = sorted(zip(scores, population), key=lambda x: x[0], reverse=True)
    survivors = [p for _, p in ranked[:top_k]]

    new_pop = list(survivors)
    while len(new_pop) < pop_size:
        parent = random.choice(survivors)
        child = mutate(parent)
        new_pop.append(child)

    # Tag each variant with its index in the new population
    for i, variant in enumerate(new_pop):
        variant["variant_id"] = i

    return new_pop


def backup_best_params(current_params: dict) -> None:
    date_str = datetime.date.today().isoformat()
    backup_path = os.path.join(BACKUP_DIR, f"params_backup_{date_str}.json")
    with open(backup_path, "w") as f:
        json.dump(current_params, f, indent=2)


def promote_best(population: list[dict], scores: list[float]) -> dict:
    """Write the best-performing variant's params to strategy_params.json."""
    best_idx = scores.index(max(scores))
    best = copy.deepcopy(population[best_idx])
    best.pop("variant_id", None)  # don't pollute the production config

    # Backup the current live params before overwriting
    if os.path.exists(BEST_PARAMS_PATH):
        with open(BEST_PARAMS_PATH) as f:
            current = json.load(f)
        backup_best_params(current)

    with open(BEST_PARAMS_PATH, "w") as f:
        json.dump(best, f, indent=2)

    return best


def main():
    print(f"[{datetime.datetime.utcnow().isoformat()}Z] === Weekly Evolution Run ===")

    population = load_population()
    pop_size = len(population)

    # Score each variant
    scores = []
    for i, variant in enumerate(population):
        score = score_variant(i)
        scores.append(score)
        print(f"  Variant {i:02d} | score={score:.4f} | trailing_stop={variant.get('trailing_stop_pct')}%")

    best_score = max(scores)
    best_idx = scores.index(best_score)
    print(f"\nBest this week: Variant {best_idx} | score={best_score:.4f}")

    # Evolve next generation
    next_gen = evolve_population(population, scores, pop_size=pop_size, top_k=3)
    save_population(next_gen)
    print(f"Evolved new population of {len(next_gen)} variants.")

    # Promote best to live config
    promoted = promote_best(population, scores)
    print(f"Promoted to strategy_params.json: {json.dumps(promoted, indent=2)}")

    print(f"[{datetime.datetime.utcnow().isoformat()}Z] === Evolution complete ===\n")


if __name__ == "__main__":
    main()
