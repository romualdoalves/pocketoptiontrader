"""
trade_logger.py — Structured trade logging for all strategies.

Every entry/exit is written as a JSON line to logs/trade_log.jsonl.
Call log_trade() when a position opens, log_outcome() when it closes.
"""

import json
import datetime
import os
import uuid

LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "trade_log.jsonl")


def log_trade(
    ticker: str,
    action: str,
    qty: float,
    price: float,
    strategy: str,
    params: dict,
    notes: str = "",
) -> str:
    """
    Append an opening trade record to the trade log.

    Args:
        ticker:   Stock or option symbol (e.g. "TSLA", "TSLA240119C00250000")
        action:   "BUY" or "SELL"
        qty:      Number of shares or contracts
        price:    Fill price
        strategy: "trailing_stop" | "ladder" | "wheel" | "copy_trading"
        params:   Snapshot of STRATEGY_PARAMS at time of trade
        notes:    Any extra context (e.g. "ladder level 1", "CSP entry")

    Returns:
        trade_id: UUID string for later cross-referencing with log_outcome()
    """
    trade_id = str(uuid.uuid4())
    record = {
        "trade_id":  trade_id,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "ticker":    ticker,
        "action":    action,
        "qty":       qty,
        "price":     price,
        "strategy":  strategy,
        "params":    params,
        "notes":     notes,
    }
    _append(record)
    return trade_id


def log_outcome(
    trade_id: str,
    exit_price: float,
    pnl_pct: float,
    hold_days: float,
    market_regime: str,
    notes: str = "",
) -> None:
    """
    Append an outcome record that pairs with a prior log_trade() call.

    Args:
        trade_id:      UUID returned by log_trade()
        exit_price:    Price at which position was closed
        pnl_pct:       Profit/loss as a percentage (e.g. 3.5 means +3.5%)
        hold_days:     How many calendar days the position was held
        market_regime: Regime at exit: "trending_bull" | "trending_bear" |
                       "ranging" | "volatile"
        notes:         Reason for exit (e.g. "trailing stop triggered",
                       "option expired worthless", "manually closed")
    """
    record = {
        "trade_id":      trade_id,
        "timestamp":     datetime.datetime.utcnow().isoformat() + "Z",
        "type":          "OUTCOME",
        "exit_price":    exit_price,
        "pnl_pct":       pnl_pct,
        "hold_days":     hold_days,
        "market_regime": market_regime,
        "notes":         notes,
    }
    _append(record)


def load_trade_log(strategy: str = None, last_n: int = None) -> list[dict]:
    """
    Load all records from the trade log.

    Args:
        strategy: If provided, filter to only this strategy's trades.
        last_n:   If provided, return only the last N records.

    Returns:
        List of trade dicts, chronological order.
    """
    if not os.path.exists(LOG_PATH):
        return []

    records = []
    with open(LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    if strategy:
        records = [r for r in records if r.get("strategy") == strategy]

    if last_n:
        records = records[-last_n:]

    return records


def load_closed_trades(strategy: str = None) -> list[dict]:
    """
    Return a merged list of closed trades: each entry record joined with its
    matching OUTCOME record, containing both entry and exit fields.

    Only returns trades that have a corresponding OUTCOME entry.
    """
    records = load_trade_log()

    entries = {r["trade_id"]: r for r in records if r.get("type") != "OUTCOME"}
    outcomes = {r["trade_id"]: r for r in records if r.get("type") == "OUTCOME"}

    closed = []
    for trade_id, entry in entries.items():
        if trade_id in outcomes:
            merged = {**entry, **outcomes[trade_id]}
            closed.append(merged)

    if strategy:
        closed = [t for t in closed if t.get("strategy") == strategy]

    return closed


def _append(record: dict) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")
