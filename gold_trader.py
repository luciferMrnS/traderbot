import alpaca.trading.client as trading_client
import alpaca.trading.requests as trading_requests
import alpaca.trading.enums as trading_enums
import alpaca.data.requests as adr
import alpaca.data.enums as data_enums
import alpaca.data.timeframe as timeframe
import pandas as pd
import numpy as np
import time
import logging
import os
import json
import math
import hashlib
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from bot_state import _get_bot_state

load_dotenv()

DEFAULT_TRADING_SYMBOLS = ("GLD",)

# BTC/USD is disabled for now: we are trading GLD only. Keep this list explicit so
# a stale deploy env (or a bare symbol override) cannot restart BTC trading.
DISABLED_TRADING_SYMBOLS = frozenset({"BTC/USD"})

# US equity market timezone and end-of-week auto-close behavior.
EASTERN_TIMEZONE = ZoneInfo("America/New_York")
DEFAULT_FRIDAY_CLOSE_SYMBOLS = ("GLD",)
# Close positions this many minutes before the final session's closing bell so the
# market order has time to fill before the auction.
WEEKEND_FLATTEN_MINUTES_BEFORE_CLOSE = 10


def normalize_trading_symbol(symbol):
    normalized = str(symbol).strip().upper()
    if normalized in {"BTC/USD", "BTCUSD", "BTC-USD"}:
        return "BTC/USD"
    return normalized


def get_trading_symbols():
    configured_symbols = os.getenv("TRADING_SYMBOLS")
    if configured_symbols is None:
        configured_symbols = os.getenv("TRADING_SYMBOL")
        if configured_symbols is None:
            return list(DEFAULT_TRADING_SYMBOLS)

    symbols = [normalize_trading_symbol(symbol) for symbol in configured_symbols.split(",")]
    if not symbols or any(not symbol for symbol in symbols):
        raise ValueError("TRADING_SYMBOLS must contain one or more comma-separated symbols.")
    symbols = [
        symbol for symbol in symbols
        if symbol not in DISABLED_TRADING_SYMBOLS
    ]
    if not symbols:
        # A BTC-only config must never restart BTC trading; fall back to the
        # GLD default so the service stays up with an allowed symbol.
        symbols = list(DEFAULT_TRADING_SYMBOLS)
    return list(dict.fromkeys(symbols))


def get_friday_close_symbols():
    """Symbols that should be flattened ahead of the weekend (last trading day of the week)."""
    configured_symbols = os.getenv("FRIDAY_CLOSE_SYMBOLS")
    if configured_symbols is None:
        return list(DEFAULT_FRIDAY_CLOSE_SYMBOLS)

    symbols = [normalize_trading_symbol(symbol) for symbol in configured_symbols.split(",")]
    symbols = [symbol for symbol in symbols if symbol]
    if not symbols:
        raise ValueError("FRIDAY_CLOSE_SYMBOLS must contain one or more comma-separated symbols.")
    return list(dict.fromkeys(symbols))


def is_crypto_symbol(symbol):
    return normalize_trading_symbol(symbol) == "BTC/USD"


STRATEGY_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_state.json")
HEARTBEAT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_heartbeat.json")
_STRATEGY_STATE_LOCK = threading.Lock()
_HEARTBEAT_LOCK = threading.Lock()


def _quarantine_file(path):
    """Move an unreadable file aside so the next write starts from a clean slate."""
    try:
        os.replace(path, f"{path}.corrupt")
    except OSError:
        pass


def _load_strategy_state():
    try:
        with open(STRATEGY_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(
            "Strategy state file %s is unreadable; moving it aside and starting fresh.",
            STRATEGY_STATE_FILE,
        )
        _quarantine_file(STRATEGY_STATE_FILE)
        return {}
    if not isinstance(state, dict):
        logger.warning(
            "Strategy state file %s does not contain a JSON object; moving it aside.",
            STRATEGY_STATE_FILE,
        )
        _quarantine_file(STRATEGY_STATE_FILE)
        return {}
    return state

def _save_strategy_state(state):
    # Write atomically: a restart mid-write must not leave a truncated file that
    # would kill both workers on their next start.
    temporary_file = f"{STRATEGY_STATE_FILE}.tmp"
    with open(temporary_file, "w", encoding="utf-8") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary_file, STRATEGY_STATE_FILE)


def _save_strategy_state_for_symbol(symbol, latest_bar):
    with _STRATEGY_STATE_LOCK:
        state = _load_strategy_state()
        state[symbol] = latest_bar
        _save_strategy_state(state)


def _write_heartbeat(symbol, *, status, last_bar=None, last_signal=None, last_error=None):
    """Publish worker liveness for the dashboard.

    The strategy workers and the Streamlit dashboard run in separate processes on
    the same machine, so they share this file instead of in-process state.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    with _HEARTBEAT_LOCK:
        try:
            with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
                heartbeat = json.load(f)
            if not isinstance(heartbeat, dict):
                heartbeat = {}
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            heartbeat = {}

        entry = heartbeat.get(symbol) or {}
        entry.update(
            {
                "pid": os.getpid(),
                "thread": threading.current_thread().name,
                "status": status,
                "last_loop": timestamp,
                "last_error": last_error,
            }
        )
        if last_bar is not None:
            entry["last_bar"] = str(last_bar)
        if last_signal is not None:
            entry["last_signal"] = last_signal
        heartbeat[symbol] = entry

        temporary_file = f"{HEARTBEAT_FILE}.tmp"
        try:
            with open(temporary_file, "w", encoding="utf-8") as f:
                json.dump(heartbeat, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_file, HEARTBEAT_FILE)
        except OSError as exc:
            logger.warning("Could not write bot heartbeat: %s", exc)


# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class GoldTrader:
    def __init__(
        self, api_key=None, api_secret=None, paper=True, symbol="GLD",
        allocation_fraction=1.0,
    ):
        """
        Initialize the trader.
        If API keys are not provided, it defaults to a 'dry-run' mode using 
        synthetic data or yfinance for historical analysis.
        """
        self.symbol = normalize_trading_symbol(symbol)
        if not 0 < allocation_fraction <= 1:
            raise ValueError("allocation_fraction must be greater than 0 and at most 1.")
        self.allocation_fraction = allocation_fraction
        self.is_crypto = is_crypto_symbol(self.symbol)
        if self.is_crypto:
            logger.warning(
                "%s is long-only; exits occur on an opposing strategy signal, "
                "without broker-side stop-loss or take-profit orders.",
                self.symbol,
            )
        self.paper = paper
        self.api_key = api_key or os.getenv("ALPACA_API_KEY", "demo_key")
        self.api_secret = api_secret or os.getenv("ALPACA_API_SECRET", "demo_secret")
        
        # Initialize Alpaca Client
        try:
            self.trading_client = trading_client.TradingClient(
                api_key=self.api_key,
                secret_key=self.api_secret,
                paper=self.paper
            )
            self.crypto_data_client = None
            try:
                if self.is_crypto:
                    from alpaca.data.historical import CryptoHistoricalDataClient
                    self.crypto_data_client = CryptoHistoricalDataClient(
                        api_key=self.api_key,
                        secret_key=self.api_secret
                    )
                    self.data_client = None
                else:
                    import alpaca.data
                    self.data_client = alpaca.data.StockHistoricalDataClient(
                        api_key=self.api_key,
                        secret_key=self.api_secret
                    )
            except Exception as e:
                logger.warning("Alpaca data client initialization failed: %s", e)
                self.data_client = None
                self.crypto_data_client = None
            self.use_alpaca = True
            logger.info(f"Connected to Alpaca (Paper: {self.paper})")
        except Exception as e:
            logger.warning(f"Alpaca connection failed: {e}. Falling back to YFinance/Dry-Run mode.")
            self.use_alpaca = False
            self.trading_client = None
            self.data_client = None
            self.crypto_data_client = None

        self.last_data_source = "not fetched"

        # Strategy Parameters
        self.fast_ema = 50
        self.slow_ema = 200
        self.rsi_period = 14
        self.atr_period = 14
        self.rsi_long_trigger = 40
        self.rsi_short_trigger = 60
        self.stop_atr_multiple = 1.5
        self.target_atr_multiple = 3.0
        self.risk_pct = 0.01
        self.bar_minutes = 15

        # End-of-week auto-close (no position carried over the weekend).
        # Crypto trades 24/7, so this applies to non-crypto symbols only.
        self.auto_flatten_weekend = (
            self.symbol in get_friday_close_symbols() and not self.is_crypto
        )
        self.weekend_flatten_minutes_before_close = WEEKEND_FLATTEN_MINUTES_BEFORE_CLOSE
        self._flattened_date = None
        self._last_trading_day_cache = None
        if self.auto_flatten_weekend:
            logger.info(
                "%s will be flattened before the weekend (last trading day of the week, "
                "within %s minutes of that session's close).",
                self.symbol,
                self.weekend_flatten_minutes_before_close,
            )

    def _get_data(self, limit=1000):
        """
        Fetch enough 15-minute OHLCV bars for the 200-period EMA.
        """
        if self.use_alpaca and self.is_crypto and self.crypto_data_client:
            try:
                now = datetime.now(timezone.utc)
                request = adr.CryptoBarsRequest(
                    symbol_or_symbols=[self.symbol],
                    timeframe=timeframe.TimeFrame(15, timeframe.TimeFrameUnit.Minute),
                    start=now - timedelta(days=10),
                    end=now,
                    limit=limit,
                )
                response = self.crypto_data_client.get_crypto_bars(request)
                bars = response.data.get(self.symbol, [])
                if bars:
                    df = pd.DataFrame(
                        [bar.model_dump(exclude={"symbol"}) for bar in bars]
                    ).set_index("timestamp")
                    df.index = pd.to_datetime(df.index)
                    df.columns = [str(column).title() for column in df.columns]
                    self.last_data_source = "Alpaca Crypto"
                    return df
            except Exception as e:
                logger.error(f"Alpaca data fetch failed: {e}")
        elif self.use_alpaca and self.data_client:
            try:
                now = datetime.now(timezone.utc)
                request = adr.StockBarsRequest(
                    symbol_or_symbols=[self.symbol],
                    timeframe=timeframe.TimeFrame(15, timeframe.TimeFrameUnit.Minute),
                    start=now - timedelta(days=30),
                    end=now,
                    limit=limit,
                    feed=data_enums.DataFeed.IEX,
                )
                response = self.data_client.get_stock_bars(request)
                bars = response.data.get(self.symbol, [])
                if bars:
                    df = pd.DataFrame(
                        [bar.model_dump(exclude={"symbol"}) for bar in bars]
                    ).set_index("timestamp")
                    df.index = pd.to_datetime(df.index)
                    df.columns = [str(column).title() for column in df.columns]
                    self.last_data_source = "Alpaca IEX"
                    return df
            except Exception as e:
                logger.error(f"Alpaca data fetch failed: {e}")
        
        # Fallback: YFinance
        try:
            import yfinance as yf
            tickers_to_try = []
            s = str(self.symbol)
            if self.is_crypto:
                tickers_to_try = ["BTC-USD"]
            elif s.upper() in ['XAUUSD', 'GOLD']:
                tickers_to_try = ['GC=F', 'GLD', 'IAU']
            elif s.endswith('=X'):
                tickers_to_try = [s, 'GC=F']
            else:
                tickers_to_try = [s]
            for ticker in tickers_to_try:
                try:
                    df = yf.download(ticker, period="1mo", interval="15m", progress=False)
                    if df.empty:
                        continue
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    df.columns = [str(column).title() for column in df.columns]
                    logger.info(f"Using YFinance data from {ticker}")
                    self.last_data_source = f"Yahoo Finance ({ticker})"
                    return df
                except Exception:
                    continue
            raise ValueError("YFinance returned empty data for all tickers")
        except Exception as e:
            logger.error(f"YFinance fallback failed: {e}")
            return pd.DataFrame()

    def _calculate_indicators(self, df):
        if df.empty:
            return df

        df = df.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [str(column).title() for column in df.columns]
        required = {'High', 'Low', 'Close'}
        missing = required.difference(df.columns)
        if missing:
            raise ValueError(f"Missing OHLC columns required for indicators: {sorted(missing)}")
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        df['EMA_Fast'] = df['Close'].ewm(
            span=self.fast_ema, adjust=False, min_periods=self.fast_ema
        ).mean()
        df['EMA_Slow'] = df['Close'].ewm(
            span=self.slow_ema, adjust=False, min_periods=self.slow_ema
        ).mean()
        delta = df['Close'].diff()
        gain = delta.clip(lower=0).ewm(
            alpha=1 / self.rsi_period, adjust=False, min_periods=self.rsi_period
        ).mean()
        loss = -delta.clip(upper=0).ewm(
            alpha=1 / self.rsi_period, adjust=False, min_periods=self.rsi_period
        ).mean()
        rs = gain.div(loss.replace(0, np.nan))
        df['RSI'] = 100 - (100 / (1 + rs))
        df.loc[(loss == 0) & (gain > 0), 'RSI'] = 100
        df.loc[(loss == 0) & (gain == 0), 'RSI'] = 50

        previous_close = df['Close'].shift(1)
        true_range = pd.concat(
            [
                df['High'] - df['Low'],
                (df['High'] - previous_close).abs(),
                (df['Low'] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df['ATR'] = true_range.ewm(
            alpha=1 / self.atr_period, adjust=False, min_periods=self.atr_period
        ).mean()

        return df

    def _generate_signal(self, df):
        if len(df) < self.slow_ema + 1:
            return 'HOLD'
        last_row, prev_row = df.iloc[-1], df.iloc[-2]
        values = [
            last_row['EMA_Fast'], last_row['EMA_Slow'], last_row['RSI'], last_row['ATR'],
            prev_row['EMA_Fast'], prev_row['EMA_Slow'], prev_row['RSI'],
        ]
        if any(pd.isna(value) for value in values):
            return 'HOLD'

        if (
            last_row['EMA_Fast'] > last_row['EMA_Slow']
            and last_row['RSI'] > self.rsi_long_trigger
        ):
            logger.info("Signal: BUY (bull trend, RSI above 40)")
            return 'BUY'
        if (
            last_row['EMA_Fast'] < last_row['EMA_Slow']
            and last_row['RSI'] < self.rsi_short_trigger
        ):
            logger.info("Signal: SELL (bear trend, RSI below 60)")
            return 'SELL'
        return 'HOLD'

    def _closed_bars(self, df, now=None):
        if df.empty:
            return df
        result = df.copy()
        index = pd.DatetimeIndex(pd.to_datetime(result.index))
        if index.tz is None:
            index = index.tz_localize("America/New_York")
        else:
            index = index.tz_convert("UTC")
        result.index = index
        result = result.sort_index()
        index = result.index
        current_time = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
        if current_time.tzinfo is None:
            current_time = current_time.tz_localize("UTC")
        else:
            current_time = current_time.tz_convert("UTC")
        return result.loc[index + pd.Timedelta(minutes=self.bar_minutes) <= current_time]

    def _calculate_position_size(self, equity, price, stop_distance, buying_power):
        if equity <= 0 or price <= 0 or stop_distance <= 0 or buying_power <= 0:
            return 0
        risk_amount = equity * self.risk_pct
        risk_limited_qty = math.floor(risk_amount / stop_distance)
        buying_power_limited_qty = math.floor(
            buying_power * self.allocation_fraction / price
        )
        return max(0, min(risk_limited_qty, buying_power_limited_qty))

    def _calculate_crypto_position_size(
        self, equity, price, stop_distance, buying_power,
        min_trade_increment, min_order_size,
    ):
        if equity <= 0 or price <= 0 or stop_distance <= 0 or buying_power <= 0:
            return Decimal("0")
        if min_trade_increment <= 0 or min_order_size <= 0:
            raise ValueError("Crypto quantity increments must be positive.")

        risk_amount = equity * self.risk_pct
        allocated_buying_power = buying_power * self.allocation_fraction
        raw_qty = min(risk_amount / stop_distance, allocated_buying_power / price)
        increment = Decimal(str(min_trade_increment))
        qty = (
            Decimal(str(raw_qty)) / increment
        ).to_integral_value(rounding=ROUND_DOWN) * increment
        return qty if qty >= Decimal(str(min_order_size)) else Decimal("0")

    def _position_matches(self, position):
        """True when an Alpaca position belongs to this trader's symbol.

        Alpaca reports crypto positions without the slash (BTCUSD) while
        self.symbol is BTC/USD, so compare normalized symbols.
        """
        position_symbol = str(getattr(position, "symbol", "") or "")
        return (
            position_symbol.replace("/", "").upper()
            == self.symbol.replace("/", "").upper()
        )

    def _position_for_symbol(self):
        """Return this trader's open position, or None if flat."""
        return next(
            (
                position
                for position in self.trading_client.get_all_positions()
                if self._position_matches(position)
            ),
            None,
        )

    def _open_symbol_orders(self):
        return self.trading_client.get_orders(
            filter=trading_requests.GetOrdersRequest(
                status=trading_enums.QueryOrderStatus.OPEN,
                limit=100,
                symbols=[self.symbol],
            )
        )

    def close_active_order(self, symbol=None):
        """Cancel any currently open order for the symbol and return the cancelled IDs."""
        symbol = self.symbol if symbol is None else symbol
        return self.close_active_orders(symbol)

    def close_active_orders(self, symbol=None):
        symbol = self.symbol if symbol is None else symbol
        if not self.use_alpaca or not self.trading_client:
            logger.info("No Alpaca client available; cannot close %s orders.", symbol)
            return []

        try:
            open_orders = self.trading_client.get_orders(
                filter=trading_requests.GetOrdersRequest(
                    status=trading_enums.QueryOrderStatus.OPEN,
                    limit=100,
                    symbols=[symbol],
                )
            )
        except Exception as exc:
            logger.error("Failed to fetch open %s orders before closing: %s", symbol, exc)
            return []

        cancelled = []
        for order in open_orders:
            order_id = getattr(order, "id", None)
            if order_id is None:
                continue
            try:
                logger.info("Canceling open %s order %s before closing.", symbol, order_id)
                self.trading_client.cancel_order_by_id(order_id)
                cancelled.append(order_id)
            except Exception as exc:
                logger.warning("Could not cancel open %s order %s: %s", symbol, order_id, exc)
        return cancelled

    def _close_position_for_reversal(self):
        cancelled_ids = self.close_active_orders()
        if cancelled_ids:
            logger.info("Cancelled active %s order IDs before reversing: %s", self.symbol, cancelled_ids)

        open_orders = self._open_symbol_orders()

        cancel_deadline = time.monotonic() + 30
        while self._open_symbol_orders():
            if time.monotonic() >= cancel_deadline:
                logger.error("Timed out waiting for %s orders to cancel; will not reverse.", self.symbol)
                return False
            time.sleep(1)

        if self.is_crypto:
            position = self._position_for_symbol()
            if position is None:
                logger.error("No %s position remains to close.", self.symbol)
                return False
            position_side = str(position.side).split(".")[-1].lower()
            close_side = (
                trading_enums.OrderSide.SELL
                if position_side == "long"
                else trading_enums.OrderSide.BUY
            )
            close_order = self.trading_client.submit_order(
                trading_requests.MarketOrderRequest(
                    symbol=self.symbol,
                    qty=float(position.qty),
                    side=close_side,
                    type=trading_enums.OrderType.MARKET,
                    time_in_force=trading_enums.TimeInForce.GTC,
                )
            )
        else:
            close_order = self.trading_client.close_position(self.symbol)
        close_order_id = getattr(close_order, "id", None)
        if close_order_id is None:
            logger.error("Alpaca did not return an order ID for the %s position close.", self.symbol)
            return False

        close_deadline = time.monotonic() + 30
        while time.monotonic() < close_deadline:
            order = self.trading_client.get_order_by_id(close_order_id)
            status = str(order.status).split(".")[-1].lower()
            positions = self.trading_client.get_all_positions()
            position_still_open = any(
                self._position_matches(position) for position in positions
            )
            if status == "filled" and not position_still_open:
                logger.info("Closed existing %s position before reversing.", self.symbol)
                return True
            if status in {"canceled", "rejected", "expired"}:
                logger.error("Position close order %s ended with status %s.", close_order_id, status)
                return False
            time.sleep(1)

        logger.error(
            "Timed out waiting for close order %s to fill; will not open the counter-position.",
            close_order_id,
        )
        return False

    def _execute_trade(self, signal, df, signal_bar_time=None):
        # Hard stop: never open a BTC/USD position from the strategy, even if a
        # trader instance was created directly with that symbol.
        if self.symbol in DISABLED_TRADING_SYMBOLS:
            logger.info("Skipping %s signal: %s trading is disabled.", signal, self.symbol)
            return False

        last = df.iloc[-1]
        current_price = float(last['Close'])
        atr = float(last['ATR'])
        stop_distance = atr * self.stop_atr_multiple
        target_distance = atr * self.target_atr_multiple
        if not math.isfinite(current_price) or not math.isfinite(stop_distance) or stop_distance <= 0:
            logger.warning("Skipping signal because price or ATR is invalid.")
            return False

        if not self.use_alpaca or not self.trading_client:
            logger.info(f"[DRY-RUN] Signal: {signal} @ ${current_price:.2f}, ATR: ${atr:.2f}")
            return True

        try:
            if self.is_crypto:
                clock_time = pd.Timestamp.now(tz="UTC")
            else:
                clock = self.trading_client.get_clock()
                if not clock.is_open:
                    logger.info("Market is closed. Skipping order submission.")
                    return False
                clock_time = pd.Timestamp(clock.timestamp)
            bar_closed_at = pd.Timestamp(df.index[-1]) + pd.Timedelta(minutes=self.bar_minutes)
            if clock_time.tzinfo is None:
                clock_time = clock_time.tz_localize("UTC")
            if clock_time - bar_closed_at > pd.Timedelta(minutes=30):
                logger.warning("Latest completed bar is stale; skipping order submission.")
                return False

            position = self._position_for_symbol()
            if self.is_crypto and signal == "SELL" and position is None:
                logger.info("Ignoring SELL signal; Alpaca spot crypto trading is long-only.")
                return False
            if position:
                position_side = str(position.side).split(".")[-1].lower()
                is_counter_signal = (
                    (signal == "BUY" and position_side == "short")
                    or (signal == "SELL" and position_side == "long")
                )
                if not is_counter_signal:
                    logger.info("Already in a %s position; skipping duplicate signal.", position_side)
                    return False
                if self.is_crypto:
                    logger.info("Opposing signal detected; closing the %s crypto position.", position_side)
                else:
                    logger.info("Counter-signal detected; closing %s before reversal.", position_side)
                if not self._close_position_for_reversal():
                    return False
                if self.is_crypto:
                    return True

            open_orders = self._open_symbol_orders()
            if open_orders:
                logger.warning(
                    "Open %s order(s) remain for %s; skipping new entry.",
                    len(open_orders),
                    self.symbol,
                )
                return False

            if self._position_for_symbol() is not None:
                logger.error(
                    "A %s position remains open; refusing to submit the counter-position.",
                    self.symbol,
                )
                return False

            account = self.trading_client.get_account()
            equity = float(account.equity)
            buying_power = float(
                getattr(account, "non_marginable_buying_power", None)
                if self.is_crypto and getattr(account, "non_marginable_buying_power", None) is not None
                else account.buying_power
            )
            if signal == 'BUY':
                side = trading_enums.OrderSide.BUY
                stop_price = math.floor((current_price - stop_distance) * 100) / 100
                take_profit_price = math.ceil((current_price + target_distance) * 100) / 100
            elif signal == 'SELL':
                side = trading_enums.OrderSide.SELL
                stop_price = math.ceil((current_price + stop_distance) * 100) / 100
                take_profit_price = math.floor((current_price - target_distance) * 100) / 100
            else:
                return False

            if not self.is_crypto and (stop_price <= 0 or take_profit_price <= 0):
                logger.warning("Calculated bracket prices are invalid; skipping order.")
                return False

            actual_stop_distance = abs(current_price - stop_price)
            if self.is_crypto:
                asset = self.trading_client.get_asset(self.symbol.replace("/", ""))
                asset_data = asset if isinstance(asset, dict) else asset.model_dump()
                min_trade_increment = asset_data.get("min_trade_increment")
                min_order_size = asset_data.get("min_order_size")
                if min_trade_increment is None or min_order_size is None:
                    logger.error("Alpaca did not provide quantity limits for %s; skipping order.", self.symbol)
                    return False
                qty = self._calculate_crypto_position_size(
                    equity,
                    current_price,
                    stop_distance,
                    buying_power,
                    float(min_trade_increment),
                    float(min_order_size),
                )
                if qty <= 0:
                    logger.warning("Risk or buying-power limit is below the minimum %s order size.", self.symbol)
                    return False
            else:
                qty = self._calculate_position_size(
                    equity, current_price, actual_stop_distance, buying_power
                )
                if qty < 1:
                    logger.warning("Risk or buying-power limit allows less than one share; skipping.")
                    return False

            bar_id = str(signal_bar_time or df.index[-1])
            digest = hashlib.sha256(
                f"{self.symbol}:{signal}:{bar_id}".encode("utf-8")
            ).hexdigest()[:24]
            if self.is_crypto:
                order_request = trading_requests.MarketOrderRequest(
                    symbol=self.symbol,
                    qty=float(qty),
                    side=side,
                    type=trading_enums.OrderType.MARKET,
                    time_in_force=trading_enums.TimeInForce.GTC,
                    client_order_id=f"ema-rsi-{digest}",
                )
            else:
                order_request = trading_requests.MarketOrderRequest(
                    symbol=self.symbol,
                    qty=qty,
                    side=side,
                    type=trading_enums.OrderType.MARKET,
                    time_in_force=trading_enums.TimeInForce.GTC,
                    order_class=trading_enums.OrderClass.BRACKET,
                    take_profit=trading_requests.TakeProfitRequest(
                        limit_price=take_profit_price
                    ),
                    stop_loss=trading_requests.StopLossRequest(stop_price=stop_price),
                    client_order_id=f"ema-rsi-{digest}",
                )
            order = self.trading_client.submit_order(order_request)
            if self.is_crypto:
                logger.info(
                    "Submitted %s crypto market order: qty=%s %s, entry~$%.2f, order=%s",
                    signal,
                    qty,
                    self.symbol,
                    current_price,
                    order.id,
                )
            else:
                logger.info(
                    "Submitted %s bracket: qty=%s %s, entry~$%.2f, stop=$%.2f, target=$%.2f, order=%s",
                    signal,
                    qty,
                    self.symbol,
                    current_price,
                    stop_price,
                    take_profit_price,
                    order.id,
                )
            return True
        except Exception as e:
            logger.error(f"Trade execution failed: {e}", exc_info=True)
            return False

    def _et_now(self):
        """Current time in the US Eastern timezone (timezone-aware)."""
        return datetime.now(timezone.utc).astimezone(EASTERN_TIMEZONE)

    def _is_last_trading_day_of_week(self, et_now):
        """True if today (ET) is the final equity session before the weekend.

        Uses the Alpaca trading calendar so a Friday holiday is handled correctly:
        the final session is then Thursday (or whichever weekday precedes the weekend).
        """
        today = et_now.date()
        cached = self._last_trading_day_cache
        if cached is not None and cached[0] == today:
            return cached[1]

        days_to_sunday = 6 - today.weekday()  # Monday=0 ... Sunday=6
        week_end = today + timedelta(days=days_to_sunday)
        try:
            calendar = self.trading_client.get_calendar(
                filters=trading_requests.GetCalendarRequest(start=today, end=week_end)
            )
        except Exception as exc:
            logger.warning("Could not fetch trading calendar for weekend flatten: %s", exc)
            return False

        if isinstance(calendar, dict):
            logger.warning("Unexpected trading calendar response: %s", calendar)
            return False

        trading_days = [entry.date for entry in calendar if getattr(entry, "date", None)]
        if today not in trading_days:
            self._last_trading_day_cache = (today, False)
            return False

        result = max(trading_days) == today
        self._last_trading_day_cache = (today, result)
        return result

    def _should_flatten_for_weekend(self, et_now):
        """True when it is time to close positions ahead of the weekend."""
        if not self.auto_flatten_weekend:
            return False
        if not self.use_alpaca or not self.trading_client:
            return False
        if not self._is_last_trading_day_of_week(et_now):
            return False

        try:
            clock = self.trading_client.get_clock()
        except Exception as exc:
            logger.warning("Could not read market clock for weekend flatten: %s", exc)
            return False
        if not clock.is_open or clock.next_close is None or clock.timestamp is None:
            return False

        next_close = clock.next_close
        timestamp = clock.timestamp
        if next_close.tzinfo is None:
            next_close = next_close.replace(tzinfo=timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        minutes_to_close = (next_close - timestamp).total_seconds() / 60.0
        return minutes_to_close <= self.weekend_flatten_minutes_before_close

    def _close_position_for_weekend(self):
        """Cancel open orders and close any open position ahead of the weekend."""
        logger.info("Flattening %s ahead of the weekend.", self.symbol)
        cancelled = self.close_active_orders()
        if cancelled:
            logger.info(
                "Cancelled open %s orders before weekend close: %s", self.symbol, cancelled
            )

        cancel_deadline = time.monotonic() + 30
        while self._open_symbol_orders():
            if time.monotonic() >= cancel_deadline:
                logger.error(
                    "Timed out waiting for %s orders to cancel; weekend flatten aborted.",
                    self.symbol,
                )
                return False
            time.sleep(1)

        try:
            position = self._position_for_symbol()
        except Exception as exc:
            logger.error("Failed to read %s positions for weekend close: %s", self.symbol, exc)
            return False

        if position is None:
            logger.info("No open %s position to close for the weekend.", self.symbol)
            return True

        try:
            close_order = self.trading_client.close_position(self.symbol)
        except Exception as exc:
            logger.error(
                "Failed to submit %s weekend close: %s", self.symbol, exc, exc_info=True
            )
            return False

        close_order_id = getattr(close_order, "id", None)
        if close_order_id is None:
            logger.error(
                "Alpaca did not return an order id for the %s weekend close.", self.symbol
            )
            return False

        close_deadline = time.monotonic() + 60
        while time.monotonic() < close_deadline:
            try:
                order = self.trading_client.get_order_by_id(close_order_id)
                still_open = any(
                    self._position_matches(open_position)
                    for open_position in self.trading_client.get_all_positions()
                )
            except Exception as exc:
                logger.warning("Could not poll %s weekend close order: %s", self.symbol, exc)
                time.sleep(1)
                continue

            status = str(getattr(order, "status", "")).split(".")[-1].lower()
            if status == "filled" and not still_open:
                logger.info("Closed %s position ahead of the weekend.", self.symbol)
                return True
            if status in {"canceled", "rejected", "expired"}:
                logger.error(
                    "Weekend close order %s for %s ended with status %s.",
                    close_order_id,
                    self.symbol,
                    status,
                )
                return False
            time.sleep(1)

        logger.error(
            "Timed out waiting for %s weekend close order %s to fill.",
            self.symbol,
            close_order_id,
        )
        return False

    def run(self, loop=True, interval=60):
        logger.info(f"Starting trader for {self.symbol}...")
        strategy_state = _load_strategy_state()
        last_processed = strategy_state.get(self.symbol)
        _write_heartbeat(self.symbol, status="starting", last_bar=last_processed)

        while loop:
            try:
                if not _get_bot_state():
                    _write_heartbeat(self.symbol, status="paused")
                    logger.info("Bot paused via dashboard. Waiting...")
                    time.sleep(10)
                    continue

                _write_heartbeat(self.symbol, status="running")
                et_now = self._et_now()
                weekend_halt = False
                if self.auto_flatten_weekend:
                    in_close_window = self._should_flatten_for_weekend(et_now)
                    if in_close_window and self._flattened_date != et_now.date():
                        logger.info(
                            "Final session of the week and near the close; flattening %s "
                            "so no position is carried over the weekend.",
                            self.symbol,
                        )
                        if self._close_position_for_weekend():
                            self._flattened_date = et_now.date()
                        else:
                            logger.warning(
                                "Weekend flatten of %s did not complete; retrying before the close.",
                                self.symbol,
                            )
                    weekend_halt = in_close_window or self._flattened_date == et_now.date()

                logger.info("Fetching data...")
                df = self._closed_bars(self._get_data(limit=1000))
                if df.empty:
                    logger.warning("No completed 15-minute bars received. Retrying.")
                    _write_heartbeat(self.symbol, status="no-data")
                    time.sleep(30)
                    continue

                df = self._calculate_indicators(df)
                latest_bar = df.index[-1].isoformat()
                if last_processed is None:
                    logger.info("Initializing at latest closed bar; waiting for the next bar before trading.")
                    last_processed = latest_bar
                    strategy_state[self.symbol] = latest_bar
                    _save_strategy_state_for_symbol(self.symbol, latest_bar)
                    _write_heartbeat(self.symbol, status="running", last_bar=latest_bar)
                elif latest_bar != last_processed:
                    logger.info(
                        "Latest closed 15-minute bar: %s, close=$%.2f",
                        latest_bar,
                        float(df.iloc[-1]['Close']),
                    )
                    signal = self._generate_signal(df)
                    if signal in ('BUY', 'SELL'):
                        if weekend_halt:
                            logger.info(
                                "Skipping %s signal: %s is flat for the weekend and no new "
                                "position will be opened before the next week.",
                                signal,
                                self.symbol,
                            )
                        else:
                            self._execute_trade(signal, df, latest_bar)
                    last_processed = latest_bar
                    _save_strategy_state_for_symbol(self.symbol, latest_bar)
                    _write_heartbeat(
                        self.symbol,
                        status="running",
                        last_bar=latest_bar,
                        last_signal=signal,
                    )

                if not loop:
                    break

                logger.info(f"Waiting {interval} seconds for the next data check...")
                time.sleep(interval)
                
            except KeyboardInterrupt:
                logger.info("Trader stopped by user.")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                _write_heartbeat(self.symbol, status="error", last_error=str(e))
                time.sleep(10)

if __name__ == "__main__":
    symbols = get_trading_symbols()
    allocation_fraction = 1 / len(symbols)
    traders = [
        GoldTrader(
            paper=True,
            symbol=symbol,
            allocation_fraction=allocation_fraction,
        )
        for symbol in symbols
    ]
    if len(traders) == 1:
        traders[0].run(loop=True, interval=60)
    else:
        threads = [
            threading.Thread(
                target=trader.run,
                kwargs={"loop": True, "interval": 60},
                name=f"trader-{trader.symbol.replace('/', '-')}",
                daemon=True,
            )
            for trader in traders
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()