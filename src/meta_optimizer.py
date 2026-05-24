"""
meta_optimizer.py — Claude-as-Meta-Optimizer (Approach D, Level 4).

Each Sunday evening, this script:
1. Loads the last 200 closed trades from the trade log.
2. Injects them — along with the current strategy params — into the
   meta_review_prompt.txt template.
3. Calls the Claude API and parses the JSON recommendation.
4. Backs up the current params and writes the updated values to
   config/strategy_params.json.

Schedule via cron (every Sunday at 6:30 PM, after evolve.py at 6:00 PM):
    30 18 * * 0 python3 ~/trading-bot/src/meta_optimizer.py >> ~/trading-bot/logs/meta_optimizer.log 2>&1

Prerequisites:
    pip install anthropic
    ANTHROPIC_API_KEY environment variable (or in .env)
"""

import json
import os
import sys
import datetime
import re

sys.path.insert(0, os.path.dirname(__file__))

from trade_logger import load_closed_trades

ROOT             = os.path.join(os.path.dirname(__file__), "..")
PARAMS_PATH      = os.path.join(ROOT, "config", "strategy_params.json")
PROMPT_TEMPLATE  = os.path.join(ROOT, "prompts", "meta_review_prompt.txt")
BACKUP_DIR       = os.path.join(ROOT, "config")
LOG_DIR          = os.path.join(ROOT, "logs")

MODEL            = "claude-opus-4-6"
MAX_TOKENS       = 1024
MAX_TRADE_WINDOW = 200   # last N closed trades to include in the review


def load_env():
    env_path = os.path.join(ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def load_current_params() -> dict:
    with open(PARAMS_PATH) as f:
        return json.load(f)


def build_prompt(trade_log: list[dict], current_params: dict) -> str:
    with open(PROMPT_TEMPLATE) as f:
        template = f.read()

    # Redact any sensitive fields before sending to the API
    safe_trades = []
    for t in trade_log:
        safe = {k: v for k, v in t.items() if k not in ("params",)}
        safe_trades.append(safe)

    prompt = template.replace(
        "{TRADE_LOG_JSON}",
        json.dumps(safe_trades, indent=2)
    ).replace(
        "{CURRENT_PARAMS_JSON}",
        json.dumps(current_params, indent=2)
    )
    return prompt


def call_claude(prompt: str) -> str:
    try:
        import anthropic
    except ImportError:
        raise ImportError("anthropic package not installed. Run: pip install anthropic")

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def parse_json_response(text: str) -> dict:
    """
    Extract a JSON object from Claude's response text.
    Handles cases where Claude wraps the JSON in markdown code fences.
    """
    # Strip markdown fences if present
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        json_str = match.group(1)
    else:
        # Try to find a raw JSON object in the text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            json_str = match.group(0)
        else:
            raise ValueError(f"No JSON object found in Claude response:\n{text}")

    return json.loads(json_str)


def validate_params(updated: dict, current: dict) -> dict:
    """
    Sanity-check the updated params from Claude:
    - All expected keys must be present (or inherit from current).
    - No value may deviate more than 30% from current (safety cap).
    - _notes key is preserved but not written to strategy_params.json.
    """
    notes = updated.pop("_notes", None)
    validated = {}

    for key, current_val in current.items():
        if key not in updated:
            print(f"  [warn] Claude omitted '{key}' — keeping current value {current_val}")
            validated[key] = current_val
            continue

        new_val = updated[key]

        if isinstance(current_val, (int, float)) and isinstance(new_val, (int, float)):
            max_change = abs(current_val) * 0.30
            if abs(new_val - current_val) > max_change:
                clamped = current_val + max_change * (1 if new_val > current_val else -1)
                if isinstance(current_val, int):
                    clamped = int(round(clamped))
                print(f"  [cap] '{key}': Claude proposed {new_val}, clamped to {clamped:.4f} (±30% limit)")
                validated[key] = clamped
            else:
                validated[key] = new_val
        else:
            validated[key] = new_val

    if notes:
        print(f"\nClaude's reasoning:\n  {notes}")

    return validated


def backup_params(current: dict) -> None:
    date_str = datetime.date.today().isoformat()
    path = os.path.join(BACKUP_DIR, f"params_backup_{date_str}.json")
    with open(path, "w") as f:
        json.dump(current, f, indent=2)
    print(f"Backed up current params to {path}")


def save_params(params: dict) -> None:
    with open(PARAMS_PATH, "w") as f:
        json.dump(params, f, indent=2)
    print(f"Updated strategy_params.json")


def log_run(trades_reviewed: int, old_params: dict, new_params: dict) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    record = {
        "timestamp":      datetime.datetime.utcnow().isoformat() + "Z",
        "trades_reviewed": trades_reviewed,
        "old_params":     old_params,
        "new_params":     new_params,
        "changes":        {
            k: {"from": old_params[k], "to": new_params[k]}
            for k in old_params
            if old_params.get(k) != new_params.get(k)
        },
    }
    log_path = os.path.join(LOG_DIR, "meta_optimizer.log")
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")


def main():
    load_env()

    ts = datetime.datetime.utcnow().isoformat()
    print(f"[{ts}Z] === Meta-Optimizer Run ===")

    closed_trades = load_closed_trades()
    recent_trades = closed_trades[-MAX_TRADE_WINDOW:]
    print(f"Loaded {len(recent_trades)} closed trades (of {len(closed_trades)} total)")

    if len(recent_trades) < 5:
        print("Not enough closed trades for meaningful review (need >= 5). Exiting.")
        return

    current_params = load_current_params()
    print(f"Current params: {json.dumps(current_params)}")

    print("Building prompt and calling Claude API...")
    prompt = build_prompt(recent_trades, current_params)

    raw_response = call_claude(prompt)
    print(f"Claude responded ({len(raw_response)} chars)")

    updated_raw = parse_json_response(raw_response)
    updated_params = validate_params(updated_raw, current_params)

    print(f"\nUpdated params: {json.dumps(updated_params, indent=2)}")

    backup_params(current_params)
    save_params(updated_params)
    log_run(len(recent_trades), current_params, updated_params)

    print(f"[{datetime.datetime.utcnow().isoformat()}Z] === Meta-Optimizer complete ===\n")


if __name__ == "__main__":
    main()
