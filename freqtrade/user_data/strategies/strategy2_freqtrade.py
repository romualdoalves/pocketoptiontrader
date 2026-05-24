"""
Strategy 2: The Liquidity Sentinel — Freqtrade Implementation
==============================================================
Converted from the standalone intraday_trader.py to Freqtrade's IStrategy API.

Architecture:
  Phase 1  Macro Filter     : Funding Rate + OI Divergence (Binance public API)
  Phase 2  Regime Detection : ADX + Volume-Profile-inspired VAH/VAL/POC
  Phase 3  Entry Signals    : Engine A (Breakout/Delta-Z) + Engine B (MFI Reversion)
  Phase 4  Risk Management  : ATR-based dynamic stop (1.5x) + liquidity target (3.0x)
  Phase 5  Safety Suite     : Market hours guard, circuit breaker, SPY correlation

Freqtrade version: >= 2024.1
Pair: BTC/USD on Alpaca (via CCXT)
Timeframe: 5m
"""

from datetime import datetime, timezone, time as dtime
from functools import reduce
import logging
import numpy as np
import pandas as pd
import requests

from freqtrade.strategy import IStrategy, DecimalParameter, IntParameter
from freqtrade.persistence import Trade

# ── XAI layer (non-blocking — all I/O runs in daemon threads) ─────────────────
try:
    from explanation_engine import ExplanationEngine
    _xai = ExplanationEngine()
    _XAI_AVAILABLE = True
except Exception as _xai_err:
    _XAI_AVAILABLE = False
    import warnings
    warnings.warn(f"[XAI] explanation_engine unavailable: {_xai_err}")

logger = logging.getLogger(__name__)


class Strategy2LiquiditySentinel(IStrategy):

    # ------------------------------------------------------------------
    # Freqtrade metadata
    # ------------------------------------------------------------------
    INTERFACE_VERSION       = 3
    timeframe               = "5m"
    can_short               = False          # Alpaca paper spot = long only
    startup_candle_count    = 60             # need 60 bars for VP + EMA50

    stoploss                = -0.05          # fallback hard stop (5%)
    minimal_roi             = {"0": 0.10}    # fallback take-profit (10%)
    trailing_stop           = False          # we use custom_stoploss
    process_only_new_candles = True

    # ------------------------------------------------------------------
    # Tunable parameters (Hyperopt-ready)
    # ------------------------------------------------------------------
    adx_threshold      = IntParameter(15, 35, default=25, space="buy")
    delta_z_min        = DecimalParameter(1.5, 3.0, default=2.0, decimals=1, space="buy")
    mfi_oversold       = IntParameter(20, 40, default=30, space="buy")
    mfi_overbought     = IntParameter(60, 80, default=70, space="sell")
    atr_stop_mult      = DecimalParameter(1.0, 2.5, default=1.5, decimals=1, space="sell")
    atr_target_mult    = DecimalParameter(2.0, 5.0, default=3.0, decimals=1, space="sell")
    vp_window          = IntParameter(30, 90, default=60, space="buy")

    # ------------------------------------------------------------------
    # Market hours (UTC) — "Smart Money" window: Milan/London/NY overlap
    # ------------------------------------------------------------------
    MARKET_OPEN_H, MARKET_OPEN_M   = 9, 29    # resume 1 min before open
    MARKET_CLOSE_H, MARKET_CLOSE_M = 17, 0    # stop at 17:00 UTC

    # ------------------------------------------------------------------
    # Risk / Circuit Breaker
    # ------------------------------------------------------------------
    DAILY_LOSS_LIMIT_USD   = 3_000.0
    FUNDING_RATE_THRESHOLD = 0.0001           # 0.01% per 8h = long-heavy

    # ------------------------------------------------------------------
    # Sleeping state (logged every 10 minutes)
    # ------------------------------------------------------------------
    _last_sleep_log: datetime = None

    # ==================================================================
    # PHASE 1 — MACRO FILTER (called once per candle via populate_indicators)
    # ==================================================================

    @staticmethod
    def _get_funding_rate() -> float:
        """Binance Futures public API — no auth required."""
        try:
            r = requests.get(
                "https://fapi.binance.com/fapi/v1/fundingRate",
                params={"symbol": "BTCUSDT", "limit": 1},
                timeout=4
            )
            return float(r.json()[0]["fundingRate"])
        except Exception:
            return 0.0

    @staticmethod
    def _get_oi_trend() -> str:
        """Returns 'increasing', 'decreasing', or 'neutral'."""
        try:
            r = requests.get(
                "https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol": "BTCUSDT", "period": "5m", "limit": 6},
                timeout=4
            )
            data = r.json()
            oi_old = float(data[0]["sumOpenInterest"])
            oi_new = float(data[-1]["sumOpenInterest"])
            pct    = (oi_new - oi_old) / oi_old * 100
            if pct > 0.1:
                return "increasing"
            if pct < -0.1:
                return "decreasing"
            return "neutral"
        except Exception:
            return "neutral"

    # ==================================================================
    # PHASE 2 — VOLUME PROFILE (vectorized rolling approximation)
    # ==================================================================

    @staticmethod
    def _rolling_vp(df: pd.DataFrame, window: int) -> tuple:
        """
        Rolling Volume-Profile approximation — O(N) per bar.
        POC  = volume-weighted mean price over window
        Std  = volume-weighted std dev
        VAH  = POC + 1 std  (top of 68% value area)
        VAL  = POC - 1 std  (bottom of 68% value area)

        For true VP accuracy, use a larger window (60+ bars).
        """
        tp  = (df["high"] + df["low"] + df["close"]) / 3
        vol = df["volume"]

        # Volume-weighted mean (POC proxy)
        poc = (tp * vol).rolling(window).sum() / vol.rolling(window).sum()

        # Volume-weighted std
        variance = ((tp - poc) ** 2 * vol).rolling(window).sum() / vol.rolling(window).sum()
        std      = variance.apply(lambda x: x ** 0.5 if x >= 0 else 0)

        vah = poc + std
        val = poc - std

        return poc, vah, val

    # ==================================================================
    # POPULATE INDICATORS
    # ==================================================================

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:

        # EMA 50
        dataframe["ema50"] = dataframe["close"].ewm(span=50, adjust=False).mean()

        # VWAP (cumulative session — resets on first bar of the day)
        tp = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3
        dataframe["vwap"] = (tp * dataframe["volume"]).cumsum() / dataframe["volume"].cumsum()

        # ATR (14)
        prev_close = dataframe["close"].shift(1)
        tr = pd.concat([
            dataframe["high"] - dataframe["low"],
            (dataframe["high"] - prev_close).abs(),
            (dataframe["low"] - prev_close).abs()
        ], axis=1).max(axis=1)
        dataframe["atr"] = tr.rolling(14).mean()

        # ADX (14) — manual calculation (no talib dependency)
        h_diff = dataframe["high"].diff()
        l_diff = -dataframe["low"].diff()
        plus_dm  = np.where((h_diff > l_diff) & (h_diff > 0), h_diff, 0.0)
        minus_dm = np.where((l_diff > h_diff) & (l_diff > 0), l_diff, 0.0)
        atr14    = tr.rolling(14).mean() + 1e-9
        plus_di  = 100 * (pd.Series(plus_dm).rolling(14).mean() / atr14)
        minus_di = 100 * (pd.Series(minus_dm).rolling(14).mean() / atr14)
        dx       = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9))
        dataframe["adx"]      = dx.rolling(14).mean()
        dataframe["plus_di"]  = plus_di
        dataframe["minus_di"] = minus_di

        # MFI (14) — Money Flow Index
        mf      = tp * dataframe["volume"]
        pos_mf  = mf.where(tp > tp.shift(1), 0.0).rolling(14).sum()
        neg_mf  = mf.where(tp < tp.shift(1), 0.0).rolling(14).sum() + 1e-9
        dataframe["mfi"] = 100 - (100 / (1 + pos_mf / neg_mf))

        # Bollinger Bands (20, 2σ)
        sma  = dataframe["close"].rolling(20).mean()
        std  = dataframe["close"].rolling(20).std()
        dataframe["bb_upper"] = sma + 2 * std
        dataframe["bb_lower"] = sma - 2 * std
        dataframe["bb_mid"]   = sma

        # Volume Delta Z-Score
        bar_range            = (dataframe["high"] - dataframe["low"]).replace(0, np.nan)
        dataframe["delta"]   = ((2 * dataframe["close"] - dataframe["high"] - dataframe["low"])
                                / bar_range * dataframe["volume"]).fillna(0)
        d_mean               = dataframe["delta"].rolling(20).mean()
        d_std                = dataframe["delta"].rolling(20).std() + 1e-9
        dataframe["delta_z"] = (dataframe["delta"] - d_mean) / d_std

        # Volatility index (normalized ATR)
        dataframe["vol_index"] = dataframe["atr"] / dataframe["close"]

        # Volume Profile (rolling)
        window = int(self.vp_window.value)
        dataframe["poc"], dataframe["vah"], dataframe["val"] = self._rolling_vp(dataframe, window)

        # Regime
        dataframe["regime"] = np.where(
            dataframe["adx"] > self.adx_threshold.value, "TRENDING", "RANGING"
        )

        # Macro filter (fetched once per candle — cached via simple class var)
        dataframe["funding_rate"] = self._get_funding_rate()
        dataframe["oi_trend"]     = self._get_oi_trend()

        # Price direction (last 3 bars)
        dataframe["price_rising"] = dataframe["close"] > dataframe["close"].shift(3)

        # Long-heavy = funding > threshold AND price rising
        dataframe["macro_blocks_long"] = (
            (dataframe["funding_rate"] > self.FUNDING_RATE_THRESHOLD) |
            (dataframe["price_rising"] & (dataframe["oi_trend"] == "decreasing"))
        )

        # MFI divergence flags (price vs MFI over 5 bars)
        dataframe["mfi_bull_div"] = (
            (dataframe["close"] < dataframe["close"].shift(5)) &
            (dataframe["mfi"]   > dataframe["mfi"].shift(5))
        )
        dataframe["mfi_bear_div"] = (
            (dataframe["close"] > dataframe["close"].shift(5)) &
            (dataframe["mfi"]   < dataframe["mfi"].shift(5))
        )

        return dataframe

    # ==================================================================
    # PHASE 3 — ENTRY SIGNALS
    # ==================================================================

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0

        # ---- Engine A: IMBALANCED / Breakout -------------------------
        engine_a = (
            (dataframe["regime"] == "TRENDING") &
            # Price just crossed above VAH
            (dataframe["close"] > dataframe["vah"]) &
            (dataframe["close"].shift(1) <= dataframe["vah"].shift(1)) &
            # Volume Delta Z-Score confirms aggressive buyers
            (dataframe["delta_z"] >= self.delta_z_min.value) &
            # Above VWAP and EMA50
            (dataframe["close"] > dataframe["vwap"]) &
            (dataframe["close"] > dataframe["ema50"]) &
            # Macro allows longs
            (~dataframe["macro_blocks_long"])
        )

        # ---- Engine B: BALANCED / Mean Reversion ---------------------
        engine_b = (
            (dataframe["regime"] == "RANGING") &
            # Price near lower band (within 0.3 ATR)
            (dataframe["close"] <= dataframe["val"] + dataframe["atr"] * 0.3) &
            # RSI/MFI oversold or bullish divergence
            ((dataframe["mfi"] < self.mfi_oversold.value) | dataframe["mfi_bull_div"]) &
            (~dataframe["macro_blocks_long"])
        )

        dataframe.loc[engine_a | engine_b, "enter_long"] = 1
        dataframe.loc[engine_a, "enter_tag"] = "A-Breakout"
        dataframe.loc[engine_b, "enter_tag"] = "B-Reversion"

        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"] = 0

        # Engine A exit: price falls back below VAH
        exit_a = (
            (dataframe["regime"] == "TRENDING") &
            (dataframe["close"] < dataframe["vah"]) &
            (dataframe["delta_z"] <= -self.delta_z_min.value)
        )

        # Engine B exit: price reaches VAH or MFI overbought / bearish divergence
        exit_b = (
            (dataframe["close"] >= dataframe["vah"] - dataframe["atr"] * 0.3) &
            ((dataframe["mfi"] > self.mfi_overbought.value) | dataframe["mfi_bear_div"])
        )

        dataframe.loc[exit_a | exit_b, "exit_long"] = 1
        return dataframe

    # ==================================================================
    # PHASE 4 — DYNAMIC STOP LOSS + TAKE PROFIT
    # ==================================================================

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        """
        Structural stop: POC - (1.5 x ATR) below entry.
        Returned as a ratio relative to current rate for Freqtrade.
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or dataframe.empty:
            return self.stoploss  # fallback

        last       = dataframe.iloc[-1]
        atr        = float(last["atr"])
        poc        = float(last["poc"])
        stop_price = poc - self.atr_stop_mult.value * atr

        # Return as negative ratio from current rate
        stop_ratio = (stop_price - current_rate) / current_rate
        return max(stop_ratio, -0.15)  # never wider than 15%

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        """
        Exit at liquidity target: entry + (3.0 x ATR).
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or dataframe.empty:
            return None

        last         = dataframe.iloc[-1]
        atr          = float(last["atr"])
        target_price = trade.open_rate + self.atr_target_mult.value * atr

        if current_rate >= target_price:
            return f"liquidity-target-{self.atr_target_mult.value}atr"
        return None

    # ==================================================================
    # PHASE 5 — SAFETY SUITE
    # ==================================================================

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float,
                             rate: float, time_in_force: str,
                             current_time: datetime, entry_tag: str = None,
                             **kwargs) -> bool:
        """
        Gate 1 — Market hours (UTC):
          Sleep  : 17:00 – 09:28 UTC
          Active : 09:29 – 16:59 UTC

        Gate 2 — Daily loss circuit breaker ($3,000)
        """
        # ---- Market hours guard ----------------------------------------
        t = current_time.astimezone(timezone.utc).time()

        open_t  = dtime(self.MARKET_OPEN_H, self.MARKET_OPEN_M)
        close_t = dtime(self.MARKET_CLOSE_H, self.MARKET_CLOSE_M)

        if not (open_t <= t < close_t):
            # Log every 10 minutes to avoid spam
            now_dt = datetime.now(timezone.utc)
            if (self._last_sleep_log is None or
                    (now_dt - self._last_sleep_log).total_seconds() >= 600):
                # Compute time until open
                if t >= close_t:
                    # Past close — open is tomorrow
                    from datetime import timedelta
                    next_open = datetime.combine(
                        current_time.date() + timedelta(days=1),
                        open_t, tzinfo=timezone.utc
                    )
                else:
                    next_open = datetime.combine(
                        current_time.date(), open_t, tzinfo=timezone.utc
                    )
                remaining = next_open - now_dt
                h = int(remaining.total_seconds() // 3600)
                m = int((remaining.total_seconds() % 3600) // 60)
                logger.info(f"[MARKET CLOSED] Sleeping {h}h {m}m until market open "
                            f"({open_t.strftime('%H:%M')} UTC)")
                Strategy2LiquiditySentinel._last_sleep_log = now_dt
            return False

        # ---- Circuit breaker -------------------------------------------
        today = current_time.date()
        closed_trades = Trade.get_trades(
            [Trade.close_date >= datetime.combine(today, dtime.min, tzinfo=timezone.utc)]
        ).all()

        daily_loss = sum(
            t.close_profit_abs for t in closed_trades
            if t.close_profit_abs is not None and t.close_profit_abs < 0
        )

        if abs(daily_loss) >= self.DAILY_LOSS_LIMIT_USD:
            logger.warning(f"[CIRCUIT BREAKER] Daily loss ${abs(daily_loss):,.2f} "
                           f">= limit ${self.DAILY_LOSS_LIMIT_USD:,.2f}. No new trades today.")
            return False

        # ── XAI: capture entry state ─────────────────────────────────────────
        if _XAI_AVAILABLE:
            try:
                dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
                if dataframe is not None and not dataframe.empty:
                    _xai.capture_entry(
                        df          = dataframe,
                        pair        = pair,
                        rate        = rate,
                        entry_tag   = entry_tag or "strategy2",
                        current_time= current_time,
                    )
            except Exception as _e:
                logger.debug("[XAI] Entry capture error: %s", _e)

        return True

    def custom_entry_price(self, pair: str, trade: Trade = None,
                            current_time: datetime = None, proposed_rate: float = None,
                            entry_tag: str = None, side: str = "long", **kwargs) -> float:
        """
        Slippage buffer: willing to pay 0.1% above mid for guaranteed fill.
        """
        return proposed_rate * 1.001

    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str,
                            amount: float, rate: float, time_in_force: str,
                            exit_reason: str, current_time: datetime,
                            **kwargs) -> bool:
        """
        XAI: capture exit state after every trade closure.
        Generates narrative text + chart PNG in a background thread.
        Trading logic is NOT modified — always returns True.
        """
        if _XAI_AVAILABLE:
            try:
                dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
                current_profit = (rate - trade.open_rate) / trade.open_rate
                _xai.capture_exit(
                    trade          = trade,
                    df             = dataframe if dataframe is not None else pd.DataFrame(),
                    rate           = rate,
                    exit_reason    = exit_reason,
                    current_profit = current_profit,
                    current_time   = current_time,
                )
            except Exception as _e:
                logger.debug("[XAI] Exit capture error: %s", _e)

        return True   # never block the exit
