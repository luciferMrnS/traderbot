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
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

from bot_state import _get_bot_state

load_dotenv()

STRATEGY_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_state.json")

def _load_strategy_state():
    try:
        with open(STRATEGY_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(state, dict):
        raise ValueError(f"Invalid strategy state in {STRATEGY_STATE_FILE}")
    return state

def _save_strategy_state(state):
    with open(STRATEGY_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class GoldTrader:
    def __init__(self, api_key=None, api_secret=None, paper=True, symbol="GLD"):
        """
        Initialize the GoldTrader.
        If API keys are not provided, it defaults to a 'dry-run' mode using 
        synthetic data or yfinance for historical analysis.
        """
        self.symbol = symbol
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
            # Try to initialize data client
            try:
                import alpaca.data
                self.data_client = alpaca.data.StockHistoricalDataClient(
                    api_key=self.api_key,
                    secret_key=self.api_secret
                )
            except Exception:
                self.data_client = None
            self.use_alpaca = True
            logger.info(f"Connected to Alpaca (Paper: {self.paper})")
        except Exception as e:
            logger.warning(f"Alpaca connection failed: {e}. Falling back to YFinance/Dry-Run mode.")
            self.use_alpaca = False
            self.trading_client = None
            self.data_client = None

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

    def _get_data(self, limit=1000):
        """
        Fetch enough 15-minute OHLCV bars for the 200-period EMA.
        """
        if self.use_alpaca and self.data_client:
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
            if s.upper() in ['XAUUSD', 'GOLD']:
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
            and prev_row['RSI'] <= self.rsi_long_trigger
            and last_row['RSI'] > self.rsi_long_trigger
        ):
            logger.info("Signal: BUY (bull trend, RSI crossed above 40)")
            return 'BUY'
        if (
            last_row['EMA_Fast'] < last_row['EMA_Slow']
            and prev_row['RSI'] >= self.rsi_short_trigger
            and last_row['RSI'] < self.rsi_short_trigger
        ):
            logger.info("Signal: SELL (bear trend, RSI crossed below 60)")
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
        buying_power_limited_qty = math.floor(buying_power / price)
        return max(0, min(risk_limited_qty, buying_power_limited_qty))

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
                position.symbol == self.symbol for position in positions
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
            clock = self.trading_client.get_clock()
            if not clock.is_open:
                logger.info("Market is closed. Skipping order submission.")
                return False
            bar_closed_at = pd.Timestamp(df.index[-1]) + pd.Timedelta(minutes=self.bar_minutes)
            clock_time = pd.Timestamp(clock.timestamp)
            if clock_time.tzinfo is None:
                clock_time = clock_time.tz_localize("UTC")
            if clock_time - bar_closed_at > pd.Timedelta(minutes=30):
                logger.warning("Latest completed bar is stale; skipping order submission.")
                return False

            positions = self.trading_client.get_all_positions()
            position = next(
                (position for position in positions if position.symbol == self.symbol),
                None,
            )
            if position:
                position_side = str(position.side).split(".")[-1].lower()
                is_counter_signal = (
                    (signal == "BUY" and position_side == "short")
                    or (signal == "SELL" and position_side == "long")
                )
                if not is_counter_signal:
                    logger.info("Already in a %s position; skipping duplicate signal.", position_side)
                    return False
                logger.info("Counter-signal detected; closing %s before reversal.", position_side)
                if not self._close_position_for_reversal():
                    return False

            open_orders = self._open_symbol_orders()
            if open_orders:
                logger.warning(
                    "Open %s order(s) remain for %s; skipping new entry.",
                    len(open_orders),
                    self.symbol,
                )
                return False

            remaining_positions = self.trading_client.get_all_positions()
            if any(position.symbol == self.symbol for position in remaining_positions):
                logger.error(
                    "A %s position remains open; refusing to submit the counter-position.",
                    self.symbol,
                )
                return False

            account = self.trading_client.get_account()
            equity = float(account.equity)
            buying_power = float(account.buying_power)
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

            if stop_price <= 0 or take_profit_price <= 0:
                logger.warning("Calculated bracket prices are invalid; skipping order.")
                return False
            actual_stop_distance = abs(current_price - stop_price)
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

    def run(self, loop=True, interval=60):
        logger.info(f"Starting Gold Trader for {self.symbol}...")
        strategy_state = _load_strategy_state()
        last_processed = strategy_state.get(self.symbol)

        while loop:
            try:
                if not _get_bot_state():
                    logger.info("Bot paused via dashboard. Waiting...")
                    time.sleep(10)
                    continue
                logger.info("Fetching data...")
                df = self._get_closed_bars(self._get_data(limit=1000))
                if df.empty:
                    logger.warning("No completed 15-minute bars received. Retrying.")
                    time.sleep(30)
                    continue

                df = self._calculate_indicators(df)
                latest_bar = df.index[-1].isoformat()
                if last_processed is None:
                    logger.info("Initializing at latest closed bar; waiting for the next bar before trading.")
                    last_processed = latest_bar
                    strategy_state[self.symbol] = latest_bar
                    _save_strategy_state(strategy_state)
                elif latest_bar != last_processed:
                    logger.info(
                        "Latest closed 15-minute bar: %s, close=$%.2f",
                        latest_bar,
                        float(df.iloc[-1]['Close']),
                    )
                    signal = self._generate_signal(df)
                    if signal in ('BUY', 'SELL'):
                        self._execute_trade(signal, df, latest_bar)
                    last_processed = latest_bar
                    strategy_state[self.symbol] = latest_bar
                    _save_strategy_state(strategy_state)

                if not loop:
                    break

                logger.info(f"Waiting {interval} seconds for the next data check...")
                time.sleep(interval)
                
            except KeyboardInterrupt:
                logger.info("Trader stopped by user.")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(10)

if __name__ == "__main__":
    trader = GoldTrader(paper=True, symbol="GLD")
    trader.run(loop=True, interval=60)