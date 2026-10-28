import alpaca.trading.client as trading_client
import alpaca.trading.requests as trading_requests
import alpaca.trading.enums as trading_enums
import alpaca.data.requests as adr
import alpaca.data.timeframe as timeframe
import pandas as pd
import numpy as np
import time
import logging
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

from bot_state import _get_bot_state

load_dotenv()

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

        # Strategy Parameters
        self.fast_ma = 12
        self.slow_ma = 26
        self.rsi_period = 14
        self.rsi_overbought = 70
        self.rsi_oversold = 30
        self.position_size_pct = 0.1  # 10% of equity per trade
        self.stop_loss_pct = 0.02     # 2% stop loss
        self.take_profit_pct = 0.04   # 4% take profit

    def _get_data(self, limit=200):
        """
        Fetch historical OHLCV data. Uses Alpaca if available, otherwise YFinance.
        """
        if self.use_alpaca and self.data_client:
            try:
                request = adr.StockBarsRequest(
                    symbol_or_symbols=[self.symbol],
                    timeframe=timeframe.TimeFrame.Minute,
                    start=datetime.utcnow() - timedelta(days=30),
                    end=datetime.utcnow(),
                    limit=limit
                )
                response = self.data_client.get_stock_bars(request)
                # Alpaca returns a dict of DataFrames
                if self.symbol in response.data:
                    df = response.data[self.symbol]
                    df.index = pd.to_datetime(df.index)
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
                tickers_to_try = [s, 'GC=F']
            for ticker in tickers_to_try:
                try:
                    df = yf.download(ticker, period="1mo", interval="15m", progress=False)
                    if df.empty:
                        continue
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    logger.info(f"Using YFinance data from {ticker}")
                    return df
                except Exception:
                    continue
            raise ValueError("YFinance returned empty data for all tickers")
        except Exception as e:
            logger.error(f"YFinance fallback failed: {e}")
            return pd.DataFrame()

    def _calculate_indicators(self, df):
        """
        Calculate technical indicators using pandas-ta or manual numpy.
        """
        if df.empty:
            return df
        
        # Ensure columns are numeric
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # Calculate Simple Moving Averages
        df['SMA_Fast'] = df['Close'].rolling(window=self.fast_ma).mean()
        df['SMA_Slow'] = df['Close'].rolling(window=self.slow_ma).mean()

        # Calculate RSI
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=self.rsi_period).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))

        return df

    def _generate_signal(self, df):
        """
        Generate a trading signal based on SMA Crossover + RSI Filter.
        Returns: 'BUY', 'SELL', or 'HOLD'
        """
        if len(df) < self.slow_ma + 1:
            return 'HOLD'
            
        last_row = df.iloc[-1]
        prev_row = df.iloc[-2]
        
        # Drop NaN values for signal logic
        if pd.isna(last_row['SMA_Fast']) or pd.isna(last_row['SMA_Slow']) or pd.isna(last_row['RSI']):
            return 'HOLD'

        current_price = last_row['Close']
        current_rsi = last_row['RSI']
        fast_ma = last_row['SMA_Fast']
        slow_ma = last_row['SMA_Slow']
        
        prev_fast = prev_row['SMA_Fast']
        prev_slow = prev_row['SMA_Slow']

        # Logic: Golden Cross (Fast crosses above Slow) + RSI not overbought
        if prev_fast <= prev_slow and fast_ma > slow_ma and current_rsi < self.rsi_overbought:
            logger.info(f"Signal: BUY (SMA Cross Up, RSI: {current_rsi:.2f})")
            return 'BUY'
            
        # Logic: Death Cross (Fast crosses below Slow) + RSI not oversold
        elif prev_fast >= prev_slow and fast_ma < slow_ma and current_rsi > self.rsi_oversold:
            logger.info(f"Signal: SELL (SMA Cross Down, RSI: {current_rsi:.2f})")
            return 'SELL'
            
        return 'HOLD'

    def _calculate_position_size(self, equity, price):
        """
        Calculate number of shares to buy based on equity and risk.
        """
        if equity <= 0 or price <= 0:
            return 0
        risk_amount = equity * self.position_size_pct
        qty = risk_amount / price
        return round(qty, 2)

    def _execute_trade(self, signal, df):
        """
        Execute the trade via Alpaca or log it if in dry-run mode.
        """
        current_price = df.iloc[-1]['Close']
        
        if not self.use_alpaca or not self.trading_client:
            logger.info(f"[DRY-RUN] Signal: {signal} @ Price: ${current_price:.2f}")
            return

        try:
            account = self.trading_client.get_account()
            equity = float(account.equity)
            
            positions = self.trading_client.get_all_positions()
            position = next((pos for pos in positions if pos.symbol == self.symbol), None)
            
            if signal == 'BUY':
                if position:
                    logger.info("Already holding position. Skipping BUY.")
                    return
                    
                qty = self._calculate_position_size(equity, current_price)
                if qty == 0:
                    logger.warning("Insufficient equity or invalid price for position sizing.")
                    return
                    
                logger.info(f"Executing BUY {qty} {self.symbol} @ ${current_price:.2f}")
                
                stop_price = round(current_price * (1 - self.stop_loss_pct), 2)
                take_profit_price = round(current_price * (1 + self.take_profit_pct), 2)
                
                order_request = trading_requests.MarketOrderRequest(
                    symbol=self.symbol,
                    qty=qty,
                    side=trading_enums.OrderSide.BUY,
                    time_in_force=trading_enums.TimeInForce.DAY
                )
                
                order = self.trading_client.submit_order(order_request)
                logger.info(f"Order Submitted: ID {order.id}")
                
            elif signal == 'SELL':
                if not position:
                    logger.info("No position to close. Skipping SELL.")
                    return
                    
                qty_to_sell = float(position.qty)
                logger.info(f"Executing SELL {qty_to_sell} {self.symbol} to close position.")
                
                order_request = trading_requests.MarketOrderRequest(
                    symbol=self.symbol,
                    qty=qty_to_sell,
                    side=trading_enums.OrderSide.SELL,
                    time_in_force=trading_enums.TimeInForce.GTC
                )
                
                order = self.trading_client.submit_order(order_request)
                logger.info(f"Order Submitted: ID {order.id}")
        except Exception as e:
            logger.error(f"Trade execution failed: {e}", exc_info=True)

    def run(self, loop=True, interval=60):
        """
        Main loop. Fetches data, calculates indicators, generates signal, and executes.
        """
        logger.info(f"Starting Gold Trader for {self.symbol}...")
        
        while loop:
            try:
                if not _get_bot_state():
                    logger.info("Bot paused via dashboard. Waiting...")
                    time.sleep(10)
                    continue
                logger.info("Fetching data...")
                df = self._get_data(limit=200)
                
                if df.empty:
                    logger.warning("No data received. Retrying in 30s.")
                    time.sleep(30)
                    continue

                logger.info(f"Data received. Last Price: ${df.iloc[-1]['Close']:.2f}")
                
                df = self._calculate_indicators(df)
                signal = self._generate_signal(df)
                self._execute_trade(signal, df)
                
                if not loop:
                    break
                    
                logger.info(f"Waiting {interval} seconds for next candle...")
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