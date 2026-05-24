"""
intraday_trader.py  Strategy 2: The Liquidity Sentinel (5-Min BTC/USD)

Architecture:
  Phase 1  Macro Filter:    Funding Rate + Open Interest Divergence
  Phase 2  Regime:          Volume Profile (VAH / VAL / POC)
  Phase 3  Entry Signal:    Volume Delta Z-Score (breakout) / MFI Divergence (reversion)
  Phase 4  Risk Management: Dynamic sizing, Structural Stop, Liquidity Target
  Phase 5  Safety Suite:    Slippage buffer, News Blackout, BTC/SPY Correlation Filter

External data (no auth required):
  Binance Futures public API -> Funding Rate + Open Interest
  CryptoPanic API            -> News blackout (optional, set CRYPTOPANIC_API_KEY in .env)
  Alpaca Stock API           -> SPY bars for correlation filter

Run:
    python src/intraday_trader.py --ticker BTC/USD [--dry-run]

Requires:
    pip install alpaca-py pandas numpy requests
"""

import os, sys, json, datetime, argparse, collections
import numpy as np
import pandas as pd
import requests

ROOT      = os.path.join(os.path.dirname(__file__), "..")
LOG_PATH  = os.path.join(ROOT, "logs", "intraday_trade_log.jsonl")
DAILY_LOG = os.path.join(ROOT, "logs", "intraday_daily.jsonl")

# ---- Position rules ------------------------------------------------------------------------------------------------------------------------
BUY_AMOUNT_USD         = 1000.0   # base buy amount (scaled by volatility)
MAX_OPEN_POSITIONS     = 1        # one trade at a time
DAILY_LOSS_LIMIT_PCT   = 0.03     # 3% daily loss -> circuit breaker

# ---- Phase 1 thresholds ----------------------------------------------------------------------------------------------------------------
FUNDING_RATE_THRESHOLD = 0.0001   # 0.01% per 8h -> long-heavy market
NEWS_PAUSE_MINUTES     = 5        # pause before/after major events

# ---- Phase 2 / Volume Profile ----------------------------------------------------------------------------------------------------
VP_BINS                = 50       # price bins for volume profile
VP_VALUE_AREA_PCT      = 0.70     # 70% of volume = Value Area
VP_LOOKBACK            = 60       # bars for volume profile (5h at 5-min)

# ---- Phase 3 thresholds ----------------------------------------------------------------------------------------------------------------
DELTA_ZSCORE_MIN       = 2.0      # minimum Z-score for breakout confirmation
MFI_OVERBOUGHT         = 70
MFI_OVERSOLD           = 30
EMA_TREND_PERIOD       = 50       # EMA 50 for trend filter
VWAP_CONFIRM           = True     # price must be above VWAP for longs

# ---- Phase 4 risk --------------------------------------------------------------------------------------------------------------------------
STRUCTURAL_TICKS       = 2        # stop = POC - N ticks ($1 per tick for BTC)
TICK_SIZE              = 1.0      # $1 per tick (BTC/USD)
VOL_SCALE_FACTOR       = 1.0      # $1,000  (1 / vol_index)   tune this


# ==============================================================================
# UTILITIES
# ==============================================================================

def load_env():
    env_path = os.path.join(ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def get_clients():
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
    key    = os.environ["ALPACA_API_KEY"]
    secret = os.environ.get("ALPACA_API_SECRET") or os.environ.get("ALPACA_SECRET_KEY")
    paper  = "paper" in os.environ.get("ALPACA_BASE_URL", "paper")
    trading = TradingClient(api_key=key, secret_key=secret, paper=paper)
    stocks  = StockHistoricalDataClient(api_key=key, secret_key=secret)
    crypto  = CryptoHistoricalDataClient(api_key=key, secret_key=secret)
    return trading, stocks, crypto


def get_open_position(trading_client, ticker: str):
    symbol = ticker.replace("/", "")
    try:
        return trading_client.get_open_position(symbol)
    except Exception:
        return None


# ==============================================================================
# PHASE 1  MACRO FILTER
# ==============================================================================

def get_funding_rate() -> dict:
    """
    Fetch latest BTC funding rate from Binance Futures public API (no auth).
    Returns: {'rate': float, 'long_heavy': bool, 'error': str|None}
    """
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": "BTCUSDT", "limit": 1},
            timeout=5
        )
        data = r.json()
        rate = float(data[0]["fundingRate"])
        return {"rate": rate, "long_heavy": rate > FUNDING_RATE_THRESHOLD, "error": None}
    except Exception as e:
        return {"rate": 0.0, "long_heavy": False, "error": str(e)}


def get_oi_trend() -> dict:
    """
    Fetch Open Interest history from Binance Futures public API.
    Returns: {'trend': 'increasing'|'decreasing'|'neutral', 'error': str|None}
    Rising price + falling OI = weak move (short covering). -> No Trade.
    """
    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/openInterestHist",
            params={"symbol": "BTCUSDT", "period": "5m", "limit": 6},
            timeout=5
        )
        data = r.json()
        if len(data) < 2:
            return {"trend": "neutral", "error": "insufficient data"}
        oi_old = float(data[0]["sumOpenInterest"])
        oi_new = float(data[-1]["sumOpenInterest"])
        pct_change = (oi_new - oi_old) / oi_old * 100
        if pct_change > 0.1:
            trend = "increasing"
        elif pct_change < -0.1:
            trend = "decreasing"
        else:
            trend = "neutral"
        return {"trend": trend, "pct_change": round(pct_change, 4), "error": None}
    except Exception as e:
        return {"trend": "neutral", "pct_change": 0.0, "error": str(e)}


def macro_allows_long(funding: dict, oi: dict, price_rising: bool) -> tuple[bool, str]:
    """
    Returns (allowed: bool, reason: str)
    Blocks longs when:
      - Funding rate is long-heavy (> 0.01%)
      - Price rising but OI falling (short-covering rally, not real demand)
    """
    if funding["long_heavy"]:
        return False, f"Funding {funding['rate']*100:.4f}%  long-heavy, longs blocked"
    if price_rising and oi["trend"] == "decreasing":
        return False, "Price rising but OI falling  short-covering, no real demand"
    return True, "Macro OK"


# ==============================================================================
# PHASE 2  VOLUME PROFILE
# ==============================================================================

def compute_volume_profile(df: pd.DataFrame) -> dict:
    """
    Build a fixed-range volume profile from the bar data.
    Returns: {poc, vah, val, levels: [(price, vol), ...]}
    POC  = Point of Control (highest-volume price level)
    VAH  = Value Area High (top of 70%-volume zone)
    VAL  = Value Area Low  (bottom of 70%-volume zone)
    """
    hi   = df["high"].max()
    lo   = df["low"].min()
    span = hi - lo
    if span == 0:
        mid = (hi + lo) / 2
        return {"poc": mid, "vah": mid, "val": mid, "levels": []}

    bin_size = span / VP_BINS
    vol_map  = collections.defaultdict(float)

    for _, row in df.iterrows():
        bar_lo  = row["low"]
        bar_hi  = row["high"]
        bar_vol = row["volume"]
        bar_span = bar_hi - bar_lo
        if bar_span == 0:
            lvl = round(bar_lo / bin_size) * bin_size
            vol_map[lvl] += bar_vol
            continue
        # Distribute volume proportionally across the bar's price range
        n = max(1, int(bar_span / bin_size))
        per_bin = bar_vol / n
        for i in range(n):
            lvl = round((bar_lo + i * bin_size) / bin_size) * bin_size
            vol_map[lvl] += per_bin

    levels  = sorted(vol_map.items())          # [(price, vol), ...]
    prices  = [p for p, _ in levels]
    volumes = [v for _, v in levels]

    # POC
    poc_idx = int(np.argmax(volumes))
    poc     = prices[poc_idx]

    # Value Area (70% of total volume, expanding outward from POC)
    total_vol  = sum(volumes)
    target_vol = total_vol * VP_VALUE_AREA_PCT
    va_vol     = volumes[poc_idx]
    lo_idx     = poc_idx
    hi_idx     = poc_idx

    while va_vol < target_vol:
        add_lo = volumes[lo_idx - 1] if lo_idx > 0 else 0
        add_hi = volumes[hi_idx + 1] if hi_idx < len(prices) - 1 else 0
        if add_lo == 0 and add_hi == 0:
            break
        if add_hi >= add_lo and hi_idx < len(prices) - 1:
            hi_idx += 1
            va_vol += volumes[hi_idx]
        elif lo_idx > 0:
            lo_idx -= 1
            va_vol += volumes[lo_idx]
        else:
            break

    return {
        "poc":    round(poc, 2),
        "vah":    round(prices[hi_idx], 2),
        "val":    round(prices[lo_idx], 2),
        "levels": levels,
    }


def detect_regime(price: float, vp: dict) -> str:
    """BALANCED if price is inside Value Area, IMBALANCED if outside."""
    if vp["val"] <= price <= vp["vah"]:
        return "BALANCED"
    return "IMBALANCED"


def next_liquidity_shelf(price: float, direction: str, vp: dict,
                         n_shelves: int = 3) -> float:
    """
    Find the next high-volume price shelf above (long) or below (short) current price.
    Used as the take-profit target (Liquidity Magnet).
    """
    levels = vp["levels"]
    if not levels:
        return price * (1.003 if direction == "long" else 0.997)

    prices  = np.array([p for p, _ in levels])
    volumes = np.array([v for _, v in levels])

    if direction == "long":
        mask    = prices > price
    else:
        mask    = prices < price

    if not np.any(mask):
        return price * (1.002 if direction == "long" else 0.998)

    candidate_prices  = prices[mask]
    candidate_volumes = volumes[mask]

    # Sort by volume descending, take the closest high-volume shelf
    top_idx = np.argsort(candidate_volumes)[::-1][:n_shelves]
    shelf_prices = candidate_prices[top_idx]

    if direction == "long":
        return float(np.min(shelf_prices))   # nearest shelf above
    else:
        return float(np.max(shelf_prices))   # nearest shelf below


# ==============================================================================
# PHASE 3  INDICATORS & ENTRY SIGNALS
# ==============================================================================

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    v = df["volume"].astype(float)

    # EMA 50
    df["ema50"] = c.ewm(span=EMA_TREND_PERIOD, adjust=False).mean()

    # VWAP (cumulative session approximation)
    tp = (h + l + c) / 3
    df["vwap"] = (tp * v).cumsum() / v.cumsum()

    # ATR (14)
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    # MFI  Money Flow Index (14)
    mf  = tp * v
    pos = mf.where(tp > tp.shift(1), 0).rolling(14).sum()
    neg = mf.where(tp < tp.shift(1), 0).rolling(14).sum() + 1e-9
    df["mfi"] = 100 - (100 / (1 + pos / neg))

    # Volume Delta (approximation from OHLCV)
    # Interpretation: positive = more buying pressure, negative = more selling
    bar_range = (h - l).replace(0, np.nan)
    df["delta"] = ((2 * c - h - l) / bar_range * v).fillna(0)

    # Volume Delta Z-Score (rolling 20 bars)
    delta_mean = df["delta"].rolling(20).mean()
    delta_std  = df["delta"].rolling(20).std() + 1e-9
    df["delta_zscore"] = (df["delta"] - delta_mean) / delta_std

    # Volatility Index (normalized ATR as % of price)
    df["vol_index"] = df["atr"] / c

    return df


def check_mfi_divergence(df: pd.DataFrame) -> dict:
    """
    Detect RSI/MFI divergence over last 10 bars.
    Bearish divergence: price makes higher high but MFI makes lower high -> exit long.
    Bullish divergence: price makes lower low but MFI makes higher low  -> enter long.
    """
    recent = df.tail(10)
    price_high1 = recent["close"].iloc[-1]
    price_high2 = recent["close"].iloc[-5]
    mfi_high1   = recent["mfi"].iloc[-1]
    mfi_high2   = recent["mfi"].iloc[-5]

    bearish = price_high1 > price_high2 and mfi_high1 < mfi_high2
    bullish = price_high1 < price_high2 and mfi_high1 > mfi_high2

    return {"bearish": bearish, "bullish": bullish}


def get_signal(df: pd.DataFrame, regime: str, vp: dict,
               macro_ok_long: bool) -> dict:
    """
    Engine A (IMBALANCED/breakout): Volume Delta Z-Score + VWAP + EMA50
    Engine B (BALANCED/reversion):  MFI divergence + HVN touch
    Returns: {action, reason, engine}
    """
    row   = df.iloc[-1]
    prev  = df.iloc[-2]
    price = float(row["close"])

    delta_z = float(row["delta_zscore"])
    mfi     = float(row["mfi"])
    vwap    = float(row["vwap"])
    ema50   = float(row["ema50"])
    atr     = float(row["atr"])

    div = check_mfi_divergence(df)

    # ---- Engine A: IMBALANCED  Breakout / Momentum --------------------------------------------------------
    if regime == "IMBALANCED":
        # Long breakout above VAH
        if (price > vp["vah"]
                and float(prev["close"]) <= vp["vah"]  # just crossed
                and delta_z >= DELTA_ZSCORE_MIN         # aggressive buyers confirmed
                and price > vwap                        # above VWAP
                and price > ema50                       # above EMA50
                and macro_ok_long):
            return {
                "action": "buy",
                "engine": "A-Breakout",
                "reason": (f"Breakout above VAH {vp['vah']:.0f} | "
                           f"Z={delta_z:.2f} | price>{vwap:.0f} VWAP | "
                           f"price>{ema50:.0f} EMA50"),
            }

        # Short breakdown below VAL (no macro restriction for shorts)
        if (price < vp["val"]
                and float(prev["close"]) >= vp["val"]
                and delta_z <= -DELTA_ZSCORE_MIN
                and price < vwap
                and price < ema50):
            return {
                "action": "sell",
                "engine": "A-Breakdown",
                "reason": (f"Breakdown below VAL {vp['val']:.0f} | "
                           f"Z={delta_z:.2f} | price<{vwap:.0f} VWAP"),
            }

    # ---- Engine B: BALANCED  Mean Reversion --------------------------------------------------------------------
    else:
        # Buy at VAL with bullish MFI divergence
        if (price <= vp["val"] + atr * 0.3   # within 0.3 ATR of VAL
                and (mfi < MFI_OVERSOLD or div["bullish"])
                and macro_ok_long):
            return {
                "action": "buy",
                "engine": "B-Reversion",
                "reason": (f"Price near VAL {vp['val']:.0f} | "
                           f"MFI={mfi:.1f} {'(oversold)' if mfi < MFI_OVERSOLD else ''}"
                           f"{'+ bullish divergence' if div['bullish'] else ''}"),
            }

        # Sell at VAH with bearish MFI divergence
        if (price >= vp["vah"] - atr * 0.3   # within 0.3 ATR of VAH
                and (mfi > MFI_OVERBOUGHT or div["bearish"])):
            return {
                "action": "sell",
                "engine": "B-Reversion",
                "reason": (f"Price near VAH {vp['vah']:.0f} | "
                           f"MFI={mfi:.1f} {'(overbought)' if mfi > MFI_OVERBOUGHT else ''}"
                           f"{'+ bearish divergence' if div['bearish'] else ''}"),
            }

    return {
        "action": "hold",
        "engine": f"{'A' if regime == 'IMBALANCED' else 'B'}",
        "reason": (f"No signal | regime={regime} | VAL={vp['val']:.0f} "
                   f"POC={vp['poc']:.0f} VAH={vp['vah']:.0f} | "
                   f"MFI={mfi:.1f} | Z={delta_z:.2f}"),
    }


# ==============================================================================
# PHASE 4  RISK & POSITION MANAGEMENT
# ==============================================================================

def compute_dynamic_qty(price: float, vol_index: float) -> float:
    """
    qty = (BUY_AMOUNT_USD  (1 / vol_index)) / price
    vol_index = ATR / price (normalized). High vol -> smaller position.
    Capped so we never exceed 2 or go below 0.1 the base amount.
    """
    vol_scale = 1.0 / max(vol_index, 0.0005)           # avoid div by zero
    vol_scale = max(0.1, min(2.0, vol_scale * 0.001))  # bound to [0.1, 2.0]
    dollar_amount = BUY_AMOUNT_USD * vol_scale
    qty = dollar_amount / price
    return round(qty, 6)


def structural_stop(poc: float, direction: str) -> float:
    """
    Stop = POC  (STRUCTURAL_TICKS  TICK_SIZE) for longs.
    Stop = POC + (STRUCTURAL_TICKS  TICK_SIZE) for shorts.
    If price falls back through POC, the thesis is dead.
    """
    offset = STRUCTURAL_TICKS * TICK_SIZE
    if direction == "long":
        return round(poc - offset, 2)
    return round(poc + offset, 2)


# ==============================================================================
# PHASE 5  SAFETY SUITE
# ==============================================================================

def check_news_blackout() -> dict:
    """
    Query CryptoPanic for any high-impact events in the next/last 5 minutes.
    Returns: {'blackout': bool, 'reason': str}
    Set CRYPTOPANIC_API_KEY in .env to enable. Skipped if no key.
    """
    api_key = os.environ.get("CRYPTOPANIC_API_KEY", "")
    if not api_key:
        return {"blackout": False, "reason": "No CryptoPanic key  news filter skipped"}

    try:
        r = requests.get(
            "https://cryptopanic.com/api/v1/posts/",
            params={"auth_token": api_key, "filter": "important",
                    "currencies": "BTC", "public": "true"},
            timeout=5
        )
        posts = r.json().get("results", [])
        now   = datetime.datetime.now(datetime.timezone.utc)

        for post in posts:
            published = datetime.datetime.fromisoformat(
                post["published_at"].replace("Z", "+00:00")
            )
            delta_min = abs((now - published).total_seconds() / 60)
            if delta_min <= NEWS_PAUSE_MINUTES:
                return {"blackout": True,
                        "reason": f"News blackout: '{post['title'][:60]}...' ({delta_min:.0f}m ago)"}

        return {"blackout": False, "reason": "No recent high-impact news"}
    except Exception as e:
        return {"blackout": False, "reason": f"News check failed ({e})  continuing"}


def check_spy_correlation(crypto_df: pd.DataFrame, stocks_client) -> dict:
    """
    Fetch recent SPY bars and compare direction with BTC.
    If BTC and SPY are moving in OPPOSITE directions with strong volume
    on either side -> likely a stop-hunt / dislocation event -> Stay Flat.
    Returns: {'decoupled': bool, 'reason': str}
    """
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        req = StockBarsRequest(
            symbol_or_symbols="SPY",
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start,
            limit=12,
        )
        spy_bars = stocks_client.get_stock_bars(req)
        spy_df   = spy_bars.df
        if isinstance(spy_df.index, pd.MultiIndex):
            spy_df = spy_df.loc["SPY"]
        spy_df = spy_df.reset_index().sort_values("timestamp")

        if len(spy_df) < 3 or len(crypto_df) < 3:
            return {"decoupled": False, "reason": "Not enough SPY data  filter skipped"}

        btc_ret = float(crypto_df["close"].iloc[-1]) / float(crypto_df["close"].iloc[-6]) - 1
        spy_ret = float(spy_df["close"].iloc[-1])    / float(spy_df["close"].iloc[0]) - 1

        # Decoupled: moving in opposite directions with meaningful magnitude
        if btc_ret > 0.003 and spy_ret < -0.003:
            return {"decoupled": True,
                    "reason": f"BTC +{btc_ret*100:.2f}% vs SPY {spy_ret*100:.2f}%  divergence, stay flat"}
        if btc_ret < -0.003 and spy_ret > 0.003:
            return {"decoupled": True,
                    "reason": f"BTC {btc_ret*100:.2f}% vs SPY +{spy_ret*100:.2f}%  divergence, stay flat"}

        return {"decoupled": False,
                "reason": f"BTC {btc_ret*100:.2f}% | SPY {spy_ret*100:.2f}%  correlated OK"}

    except Exception as e:
        return {"decoupled": False, "reason": f"SPY check failed ({e})  filter skipped"}


# ==============================================================================
# ORDER EXECUTION
# ==============================================================================

def place_order(trading_client, ticker: str, action: str, qty: float,
                stop_price: float, limit_price: float) -> dict:
    """
    Submit a bracket order with slippage buffer on the entry.
    Entry: LIMIT order at price  1.001 (long) or  0.999 (short).
    Bracket: structural stop + liquidity-target take-profit.
    """
    from alpaca.trading.requests import (LimitOrderRequest, OrderClass,
                                          StopLossRequest, TakeProfitRequest)
    from alpaca.trading.enums import OrderSide, TimeInForce

    side   = OrderSide.BUY if action == "buy" else OrderSide.SELL
    symbol = ticker.replace("/", "")

    # Slippage buffer: willing to pay up to 0.1% more to guarantee fill
    if action == "buy":
        entry_limit = round(limit_price * 1.001, 2)
    else:
        entry_limit = round(limit_price * 0.999, 2)

    req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        limit_price=entry_limit,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.BRACKET,
        stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
        take_profit=TakeProfitRequest(limit_price=round(limit_price, 2)),
    )

    order = trading_client.submit_order(req)
    return {"order_id": str(order.id), "qty": qty, "side": side.value,
            "entry_limit": entry_limit, "stop": stop_price, "target": limit_price}


# ==============================================================================
# DATA FETCHING
# ==============================================================================

def fetch_bars(ticker: str, limit: int = 60) -> pd.DataFrame:
    """Fetch 5-min bars with explicit 24h window to guarantee fresh data."""
    _, stocks_client, crypto_client = get_clients()
    is_crypto = "/" in ticker

    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)

    if is_crypto:
        from alpaca.data.requests import CryptoBarsRequest
        req = CryptoBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start,
            limit=limit,
        )
        bars = crypto_client.get_crypto_bars(req)
    else:
        from alpaca.data.requests import StockBarsRequest
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start,
            limit=limit,
        )
        bars = stocks_client.get_stock_bars(req)

    df = bars.df
    if isinstance(df.index, pd.MultiIndex):
        df = df.loc[ticker]
    df = df.reset_index()
    df.columns = [c.lower() for c in df.columns]
    return df.sort_values("timestamp").reset_index(drop=True)


# ==============================================================================
# DAILY CIRCUIT BREAKER
# ==============================================================================

def daily_loss_exceeded(portfolio_value: float) -> bool:
    today = datetime.date.today().isoformat()
    if not os.path.exists(DAILY_LOG):
        return False
    daily_pnl = 0.0
    with open(DAILY_LOG) as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("date") == today:
                daily_pnl += rec.get("pnl_usd", 0.0)
    return daily_pnl < -(portfolio_value * DAILY_LOSS_LIMIT_PCT)


def log_daily_pnl(pnl_usd: float) -> None:
    os.makedirs(os.path.dirname(DAILY_LOG), exist_ok=True)
    rec = {"date": datetime.date.today().isoformat(),
           "timestamp": now_utc(), "pnl_usd": pnl_usd}
    with open(DAILY_LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _log(ticker, signal, order, row, portfolio_value, extras=None, dry_run=False):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    record = {
        "timestamp":       now_utc(),
        "ticker":          ticker,
        "action":          signal["action"],
        "engine":          signal.get("engine", ""),
        "reason":          signal["reason"],
        "price":           round(float(row["close"]), 2),
        "mfi":             round(float(row["mfi"]), 2),
        "delta_zscore":    round(float(row["delta_zscore"]), 3),
        "vol_index":       round(float(row["vol_index"]), 6),
        "portfolio_value": portfolio_value,
        "order":           order,
        "dry_run":         dry_run,
        **(extras or {}),
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


# ==============================================================================
# MAIN
# ==============================================================================

def check_market_open(trading_client, ticker: str) -> dict:
    """
    Returns {'open': bool, 'next_open': datetime|None, 'reason': str}

    For crypto (BTC/USD): Alpaca crypto trades 24/7 EXCEPT on weekends
    some venues reduce liquidity. We use Alpaca's clock as a proxy for
    'high-quality trading hours' (US market open = peak BTC volume).
    If the US equity market is closed, we flag it as low-quality hours.
    The bot will warn and stop — resuming at next US market open.
    """
    try:
        clock = trading_client.get_clock()
        is_open   = clock.is_open
        next_open = clock.next_open   # datetime of next US market open

        if is_open:
            return {"open": True, "next_open": None,
                    "reason": "US market open — peak liquidity window active"}
        else:
            return {"open": False, "next_open": next_open,
                    "reason": f"US market closed. Next open: {next_open.strftime('%Y-%m-%d %H:%M UTC')}"}
    except Exception as e:
        # If clock check fails, allow trading (fail open)
        return {"open": True, "next_open": None, "reason": f"Clock check failed ({e}) — continuing"}


def run(ticker: str, dry_run: bool = False):
    load_env()
    trading_client, stocks_client, _ = get_clients()

    print(f"\n[{now_utc()}] == Liquidity Sentinel | {ticker} ==")

    # ---- Market hours check ----------------------------------------------------
    market = check_market_open(trading_client, ticker)
    if not market["open"]:
        print(f"\n[MARKET CLOSED] {market['reason']}")
        print("Bot execution stopped. Scheduler will retry at next interval.")
        print("MARKET_CLOSED")   # sentinel string read by scheduler
        return

    # Account state
    account         = trading_client.get_account()
    portfolio_value = float(account.equity)
    cash            = float(account.cash)
    print(f"Portfolio: ${portfolio_value:,.2f} | Cash: ${cash:,.2f}")

    # Circuit breaker
    if daily_loss_exceeded(portfolio_value):
        print("[X] CIRCUIT BREAKER: Daily 3% loss limit hit. No trades today.")
        return

    # ---- Phase 1: Macro Filter --------------------------------------------------------------------------------------------------
    print("\n---- Phase 1: Macro Filter")
    funding = get_funding_rate()
    oi      = get_oi_trend()

    if funding["error"]:
        print(f"  Funding: unavailable ({funding['error']})")
    else:
        print(f"  Funding rate: {funding['rate']*100:.4f}% "
              f"{'[!] LONG-HEAVY' if funding['long_heavy'] else ''}")

    if oi["error"]:
        print(f"  OI trend: unavailable ({oi['error']})")
    else:
        print(f"  OI trend: {oi['trend'].upper()} ({oi.get('pct_change', 0):+.3f}%) "
              f"{'[!]' if oi['trend'] == 'decreasing' else ''}")

    # ---- Fetch bars + indicators ----------------------------------------------------------------------------------------------
    print("\n---- Fetching bars + computing indicators")
    df    = fetch_bars(ticker, limit=VP_LOOKBACK)
    df    = compute_indicators(df)
    row   = df.iloc[-1]
    price = float(row["close"])
    print(f"  Price: ${price:,.2f} | MFI: {row['mfi']:.1f} | "
          f"Z: {row['delta_zscore']:.2f} | VolIdx: {row['vol_index']:.5f}")

    # ---- Phase 2: Volume Profile + Regime --------------------------------------------------------------------------
    print("\n---- Phase 2: Volume Profile")
    vp     = compute_volume_profile(df)
    regime = detect_regime(price, vp)
    print(f"  VAL: ${vp['val']:,.2f} | POC: ${vp['poc']:,.2f} | VAH: ${vp['vah']:,.2f}")
    print(f"  Regime: {regime}")

    # Price direction (last 3 bars)
    price_rising = float(df["close"].iloc[-1]) > float(df["close"].iloc[-4])
    macro_ok, macro_reason = macro_allows_long(funding, oi, price_rising)
    print(f"  Macro long filter: {' OK' if macro_ok else '[!] BLOCKED'}  {macro_reason}")

    # ---- Phase 5a: News Blackout ----------------------------------------------------------------------------------------------
    news = check_news_blackout()
    if news["blackout"]:
        print(f"\n[PAUSE] NEWS BLACKOUT: {news['reason']}")
        return
    else:
        print(f"\n---- Phase 5a News: {news['reason']}")

    # ---- Phase 5b: SPY Correlation ------------------------------------------------------------------------------------------
    spy = check_spy_correlation(df, stocks_client)
    print(f"---- Phase 5b SPY: {spy['reason']}")
    if spy["decoupled"]:
        print("[!] DECOUPLED  staying flat.")
        return

    # ---- Check existing position ----------------------------------------------------------------------------------------------
    open_position = get_open_position(trading_client, ticker)

    if open_position:
        open_qty   = float(open_position.qty)
        open_side  = open_position.side.value if hasattr(open_position.side, "value") \
                     else str(open_position.side)
        unrealized = float(open_position.unrealized_pl)
        print(f"\n---- Open position: {open_qty} {ticker} | {open_side.upper()} | "
              f"P&L: ${unrealized:+,.2f}")

        # ---- Phase 3: Signal (for exit) --------------------------------------------------------------------------------
        signal = get_signal(df, regime, vp, macro_ok)
        print(f"---- Phase 3 Signal [{signal['engine']}]: {signal['action'].upper()}")
        print(f"   {signal['reason']}")

        should_close = (
            (open_side == "long"  and signal["action"] == "sell") or
            (open_side == "short" and signal["action"] == "buy")
        )

        if should_close:
            print(f"-> Closing {open_side} position ({open_qty} {ticker}) | P&L: ${unrealized:+,.2f}")
            if not dry_run:
                try:
                    trading_client.close_position(ticker.replace("/", ""))
                    log_daily_pnl(unrealized)
                    _log(ticker, {"action": "close", "engine": signal["engine"],
                                  "reason": f"Exit signal: {signal['reason']}"},
                         {"closed_qty": open_qty, "pnl": unrealized}, row, portfolio_value)
                    print("   Position closed.")
                except Exception as e:
                    print(f"   Close failed: {e}")
            else:
                print("  [DRY RUN] Would close position.")
        else:
            print("-> Holding position  no exit signal yet.")
        return

    # ---- Phase 3: Signal (for entry) --------------------------------------------------------------------------------------
    print("\n---- Phase 3: Entry Signal")
    signal = get_signal(df, regime, vp, macro_ok)
    print(f"  Engine {signal['engine']}: {signal['action'].upper()}")
    print(f"  {signal['reason']}")

    if signal["action"] == "hold":
        print("-> No trade.")
        _log(ticker, signal, None, row, portfolio_value)
        return

    # ---- Phase 4: Position sizing + stops ----------------------------------------------------------------------------
    print("\n---- Phase 4: Risk Management")
    vol_index  = float(row["vol_index"])
    qty        = compute_dynamic_qty(price, vol_index)
    dollar_val = round(qty * price, 2)
    direction  = "long" if signal["action"] == "buy" else "short"

    stop   = structural_stop(vp["poc"], direction)
    target = next_liquidity_shelf(price, direction, vp)

    rr     = abs(target - price) / (abs(price - stop) + 1e-9)

    print(f"  Qty:        {qty} BTC   ${dollar_val:,.2f}  (vol_index={vol_index:.5f})")
    print(f"  Entry:      ${price:,.2f}  (limit + 0.1% slippage buffer)")
    print(f"  Stop:       ${stop:,.2f}  (structural  2 ticks below POC)")
    print(f"  Target:     ${target:,.2f}  (next liquidity shelf)")
    print(f"  R/R ratio:  1:{rr:.2f}")

    if rr < 1.0:
        print("[!] R/R below 1:1  skipping trade.")
        return

    if dry_run:
        print("\n[DRY RUN] Order not submitted.")
        _log(ticker, signal, None, row, portfolio_value,
             {"vp": vp, "stop": stop, "target": target, "qty": qty}, dry_run=True)
        return

    try:
        order = place_order(trading_client, ticker, signal["action"], qty, stop, target)
        print(f"\n Order submitted: {order}")
        _log(ticker, signal, order, row, portfolio_value,
             {"vp": vp, "stop": stop, "target": target, "qty": qty})
    except Exception as e:
        print(f" Order failed: {e}")


# ==============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Liquidity Sentinel  5-min BTC/USD")
    parser.add_argument("--ticker",  default="BTC/USD")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(ticker=args.ticker, dry_run=args.dry_run)
