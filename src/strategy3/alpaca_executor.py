"""
alpaca_executor.py — Alpaca order execution with rate-limiting, exponential
backoff, and race-condition-safe order cancellation.

Design constraints (from Alpaca crypto docs):
  • 200 requests / minute sustained, 8 / second burst.
  • Order cancel is async — need to poll until terminal state.
  • Paper and live share the same interface; only the base URL differs.
"""

from __future__ import annotations
import logging
import os
import time
import threading
from collections import deque
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest,
        GetOrdersRequest,
    )
    from alpaca.trading.enums import (
        OrderSide,
        TimeInForce,
        QueryOrderStatus,
    )
    _ALPACA_OK = True
except ImportError:
    log.warning("alpaca-py not installed — executor will run in dry-run mode only.")
    _ALPACA_OK = False


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------

class TokenBucket:
    """
    Simple thread-safe token bucket.
    `rate` tokens per second, burst up to `burst`.
    """

    def __init__(self, rate: float, burst: int):
        self.rate   = rate
        self.burst  = burst
        self.tokens = float(burst)
        self._ts    = time.monotonic()
        self._lock  = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._ts
                self._ts = now
                self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------

def _backoff(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Exponential backoff with jitter."""
    import random
    delay = min(base * (2 ** attempt), cap)
    return delay * (0.5 + random.random() * 0.5)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class AlpacaExecutor:
    """
    Stateful executor wrapping alpaca-py TradingClient.
    Handles rate limits, retries, and cancel polling.
    """

    def __init__(self, cfg: dict, dry_run: bool = True):
        self.dry_run = dry_run
        self.cfg     = cfg
        self.client: Optional["TradingClient"] = None

        rate_per_min = cfg.get("rate_limit_per_minute", 180)
        burst        = cfg.get("rate_limit_burst_per_second", 8)
        self._bucket = TokenBucket(rate=rate_per_min / 60.0, burst=burst)

        self.max_retries   = int(cfg.get("max_retries", 5))
        self.base_backoff  = float(cfg.get("base_backoff_seconds", 1.0))
        self.max_backoff   = float(cfg.get("max_backoff_seconds", 30.0))
        self.cancel_timeout = float(cfg.get("cancel_poll_timeout_seconds", 5.0))
        self.cancel_poll    = float(cfg.get("cancel_poll_interval_seconds", 0.25))
        self.slippage_buf   = float(cfg.get("slippage_buffer_pct", 0.001))

        if not dry_run and _ALPACA_OK:
            key    = os.getenv("ALPACA_API_KEY", "")
            secret = os.getenv("ALPACA_SECRET_KEY", "")
            paper  = os.getenv("ALPACA_PAPER", "true").lower() in ("1", "true", "yes")
            if key and secret:
                self.client = TradingClient(key, secret, paper=paper)
                log.info("Alpaca TradingClient initialized (paper=%s).", paper)
            else:
                log.warning("Alpaca creds missing — falling back to dry-run.")
                self.dry_run = True

    # ---- Internal rate-limited call ----

    def _call(self, fn, *args, **kwargs):
        """Call an API function with rate limiting + retries."""
        for attempt in range(self.max_retries):
            self._bucket.acquire()
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                err = str(e)
                # Rate-limit hit → back off
                if "429" in err or "rate" in err.lower():
                    wait = _backoff(attempt, self.base_backoff, self.max_backoff)
                    log.warning("Rate limited (attempt %d) — backing off %.1fs", attempt, wait)
                    time.sleep(wait)
                    continue
                # Transient server error
                if "500" in err or "502" in err or "503" in err:
                    wait = _backoff(attempt, self.base_backoff, self.max_backoff)
                    log.warning("Server error (attempt %d): %s — retry in %.1fs", attempt, err, wait)
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError(f"Exhausted {self.max_retries} retries.")

    # ---- Account / position queries ----

    def get_account(self) -> dict:
        if self.dry_run or not self.client:
            return {"equity": 10000.0, "cash": 10000.0, "dry_run": True}
        acct = self._call(self.client.get_account)
        return {
            "equity": float(acct.equity),
            "cash":   float(acct.cash),
        }

    def get_positions(self) -> Dict[str, dict]:
        if self.dry_run or not self.client:
            return {}
        positions = self._call(self.client.get_all_positions)
        result = {}
        for p in positions:
            result[p.symbol] = {
                "qty":          float(p.qty),
                "market_value": float(p.market_value),
                "avg_entry":    float(p.avg_entry_price),
                "unrealized_pl": float(p.unrealized_pl),
            }
        return result

    # ---- Order submission ----

    def submit_market_order(
        self,
        symbol: str,
        side: str,
        notional: Optional[float] = None,
        qty: Optional[float] = None,
    ) -> dict:
        """
        Submit a market order. Specify either notional (USD) or qty.
        Returns order dict (or simulated one in dry-run).
        """
        log.info("ORDER %s %s notional=%s qty=%s (dry_run=%s)",
                 side, symbol, notional, qty, self.dry_run)

        if self.dry_run or not self.client:
            return {
                "id": "dry-run",
                "symbol": symbol,
                "side": side,
                "notional": notional,
                "qty": qty,
                "status": "accepted",
                "dry_run": True,
            }

        # Alpaca crypto uses "/" in symbol (BTC/USD), but orders need it too
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        req_kwargs = {
            "symbol": symbol,
            "side": order_side,
            "time_in_force": TimeInForce.GTC,
        }
        if notional is not None:
            req_kwargs["notional"] = round(notional, 2)
        elif qty is not None:
            req_kwargs["qty"] = qty
        else:
            raise ValueError("Must provide notional or qty")

        req = MarketOrderRequest(**req_kwargs)
        order = self._call(self.client.submit_order, req)
        return {
            "id":       str(order.id),
            "symbol":   order.symbol,
            "side":     order.side.value,
            "notional": float(order.notional) if order.notional else None,
            "qty":      float(order.qty) if order.qty else None,
            "status":   order.status.value,
        }

    # ---- Cancel with polling ----

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an order and poll until terminal state.
        Returns True if successfully cancelled/filled.
        """
        if self.dry_run or not self.client:
            return True

        try:
            self._call(self.client.cancel_order_by_id, order_id)
        except Exception as e:
            if "not found" in str(e).lower() or "already" in str(e).lower():
                return True
            log.warning("Cancel request failed for %s: %s", order_id, e)
            return False

        deadline = time.monotonic() + self.cancel_timeout
        while time.monotonic() < deadline:
            try:
                order = self._call(self.client.get_order_by_id, order_id)
                status = order.status.value.lower()
                if status in ("canceled", "cancelled", "filled", "expired", "rejected"):
                    return True
            except Exception:
                return True
            time.sleep(self.cancel_poll)

        log.warning("Cancel poll timed out for order %s", order_id)
        return False

    def cancel_all_open(self) -> int:
        """Cancel all open orders. Returns count cancelled."""
        if self.dry_run or not self.client:
            return 0
        try:
            resp = self._call(self.client.cancel_orders)
            return len(resp) if resp else 0
        except Exception as e:
            log.error("cancel_all_open failed: %s", e)
            return 0

    # ---- Close position ----

    def close_position(self, symbol: str) -> dict:
        """Close entire position for a symbol."""
        if self.dry_run or not self.client:
            return {"symbol": symbol, "status": "closed", "dry_run": True}

        try:
            order = self._call(self.client.close_position, symbol)
            return {"symbol": symbol, "status": "closing", "order_id": str(order.id)}
        except Exception as e:
            if "no position" in str(e).lower():
                return {"symbol": symbol, "status": "no_position"}
            raise

    # ---- Batch rebalance ----

    def rebalance_to_targets(
        self,
        target_weights: Dict[str, float],
        equity: float,
        exposure: float,
    ) -> List[dict]:
        """
        Given target portfolio weights and total equity, compute the delta
        for each symbol and submit orders. Returns list of order results.

        exposure (0-1) scales all target notionals down.
        """
        target_notionals: Dict[str, float] = {}
        for sym, w in target_weights.items():
            target_notionals[sym] = w * equity * exposure

        positions = self.get_positions()
        current_values: Dict[str, float] = {}
        for sym, pos in positions.items():
            current_values[sym] = pos["market_value"]

        # All symbols in scope
        all_syms = set(target_notionals.keys()) | set(current_values.keys())

        orders = []
        for sym in sorted(all_syms):
            target = target_notionals.get(sym, 0.0)
            current = current_values.get(sym, 0.0)
            delta = target - current

            if abs(delta) < 5.0:  # skip dust (Alpaca min ~$1)
                continue

            if delta > 0:
                result = self.submit_market_order(sym, "buy", notional=abs(delta))
            else:
                result = self.submit_market_order(sym, "sell", notional=abs(delta))

            orders.append(result)

        # Close positions not in target
        for sym in list(current_values.keys()):
            if sym not in target_notionals and current_values[sym] > 5.0:
                orders.append(self.close_position(sym))

        return orders
