"""
scheduler.py — Self-contained 5-minute bot runner. No admin rights needed.

Runs intraday_trader.py every 5 minutes during US market hours.
When the market closes, prints a final warning and sleeps until next open.
Keeps running until you close the window or press Ctrl+C.

Usage:
    python scheduler.py                  # runs BTC/USD every 5 min
    python scheduler.py --ticker TSLA    # runs TSLA every 5 min
    python scheduler.py --dry-run        # analysis only, no real orders
"""

import subprocess
import sys
import os
import time
import datetime
import argparse
import requests

ROOT             = os.path.dirname(os.path.abspath(__file__))
SCRIPT           = os.path.join(ROOT, "src", "intraday_trader.py")
LOG              = os.path.join(ROOT, "logs", "scheduler.log")
INTERVAL_MINUTES = 5

# Alpaca clock endpoint (no auth needed for clock)
ALPACA_BASE_URL  = "https://paper-api.alpaca.markets"


def log(msg: str):
    ts   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_market_clock() -> dict:
    """
    Fetch Alpaca clock without needing auth (uses env keys if available).
    Returns dict with is_open, next_open, next_close.
    """
    env_path = os.path.join(ROOT, ".env")
    key, secret = "", ""
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "ALPACA_API_KEY=" in line:
                    key = line.split("=", 1)[1].strip()
                if "ALPACA_API_SECRET=" in line:
                    secret = line.split("=", 1)[1].strip()

    try:
        r = requests.get(
            f"{ALPACA_BASE_URL}/v2/clock",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=5
        )
        data = r.json()
        return {
            "is_open":    data.get("is_open", True),
            "next_open":  data.get("next_open", ""),
            "next_close": data.get("next_close", ""),
        }
    except Exception as e:
        # If clock unreachable, allow running (fail open)
        return {"is_open": True, "next_open": "", "next_close": ""}


def seconds_until_open(next_open_str: str) -> float:
    """Parse ISO next_open string and return seconds until then."""
    if not next_open_str:
        return 60 * 60  # fallback: wait 1 hour
    try:
        next_open = datetime.datetime.fromisoformat(next_open_str.replace("Z", "+00:00"))
        now       = datetime.datetime.now(datetime.timezone.utc)
        delta     = (next_open - now).total_seconds()
        return max(0, delta)
    except Exception:
        return 60 * 60


def next_run_time() -> datetime.datetime:
    """Return the next 5-minute boundary (:00, :05, :10, ...)."""
    now    = datetime.datetime.now()
    minutes = (now.minute // INTERVAL_MINUTES + 1) * INTERVAL_MINUTES
    delta   = datetime.timedelta(
        minutes=minutes - now.minute,
        seconds=-now.second,
        microseconds=-now.microsecond
    )
    return now + delta


def run_bot(ticker: str, dry_run: bool) -> str:
    """
    Run the bot subprocess. Returns 'MARKET_CLOSED' if market is closed,
    'OK' on normal completion, 'ERROR' on failure.
    """
    cmd = [sys.executable, SCRIPT, "--ticker", ticker]
    if dry_run:
        cmd.append("--dry-run")

    log(f"Running bot: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)

    market_closed = False
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            if "MARKET_CLOSED" in line:
                market_closed = True
            elif line.strip():
                log(f"  {line}")

    if result.stderr:
        for line in result.stderr.strip().splitlines():
            if ("DeprecationWarning" not in line
                    and "warnings" not in line
                    and line.strip()):
                log(f"  [err] {line}")

    if market_closed:
        return "MARKET_CLOSED"
    if result.returncode != 0:
        return "ERROR"
    log(f"Bot finished (exit code {result.returncode})")
    return "OK"


def sleep_until_market_open(next_open_str: str):
    """
    Log a clear warning and sleep until the market reopens.
    Wakes up 1 minute before open to be ready.
    """
    wait_secs = seconds_until_open(next_open_str) - 60  # wake 1 min early
    wait_secs = max(60, wait_secs)

    wake_time = datetime.datetime.now() + datetime.timedelta(seconds=wait_secs)
    hours     = int(wait_secs // 3600)
    minutes   = int((wait_secs % 3600) // 60)

    log("")
    log("=" * 60)
    log("[MARKET CLOSED] US market is closed.")
    log(f"  Next open: {next_open_str}")
    log(f"  Bot sleeping for {hours}h {minutes}m")
    log(f"  Will resume at: {wake_time.strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)
    log("")

    # Sleep in 10-minute chunks so Ctrl+C still works
    slept = 0
    while slept < wait_secs:
        chunk = min(600, wait_secs - slept)
        time.sleep(chunk)
        slept += chunk
        remaining = wait_secs - slept
        if remaining > 60:
            r_h = int(remaining // 3600)
            r_m = int((remaining % 3600) // 60)
            log(f"[Sleeping] {r_h}h {r_m}m until market open...")

    log("[WAKING UP] Market opens soon — resuming bot.")


def main():
    parser = argparse.ArgumentParser(description="5-min bot scheduler")
    parser.add_argument("--ticker",  default="BTC/USD")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    mode = "DRY-RUN" if args.dry_run else "LIVE"
    log(f"=== AlpacaTrader Scheduler started | {args.ticker} | {mode} | every {INTERVAL_MINUTES} min ===")
    log("Press Ctrl+C to stop.\n")

    while True:
        # Check market before running
        clock = get_market_clock()

        if not clock["is_open"]:
            # Run bot once to get the official "market closed" message in its log
            status = run_bot(args.ticker, args.dry_run)
            # Then sleep until open
            sleep_until_market_open(clock["next_open"])
            continue

        # Market is open — run the bot
        run_bot(args.ticker, args.dry_run)

        # Wait for next 5-min boundary
        nxt  = next_run_time()
        wait = (nxt - datetime.datetime.now()).total_seconds()
        log(f"Next run at {nxt.strftime('%H:%M:%S')} (in {wait:.0f}s)")
        time.sleep(max(0, wait))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n[Scheduler stopped by user]")
