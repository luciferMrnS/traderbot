import streamlit as st
import pandas as pd
import alpaca.trading.client as trading_client
import alpaca.data.historical as data_module
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from datetime import datetime, timedelta
import os
import yfinance as yf
import threading
import time
from dotenv import load_dotenv

from gold_trader import GoldTrader
from bot_state import get_bot_state, set_bot_state

load_dotenv()

# Start the trading bot in a background thread (single web service deployment)
@st.cache_resource
def start_bot_thread():
    def run_bot():
        try:
            trader = GoldTrader(paper=True, symbol="GLD")
            trader.run(loop=True, interval=60)
        except Exception as e:
            print(f"Bot thread error: {e}")
    
    thread = threading.Thread(target=run_bot, daemon=True)
    thread.start()
    return thread

# Initialize bot thread on app startup
start_bot_thread()

# Page Config
st.set_page_config(page_title="Gold Trader Dashboard", layout="wide", page_icon="🥇")

# --- Config ---
API_KEY = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_API_SECRET")
SYMBOL = "GLD"

# Try to connect
connected = False
trading_cli = None
data_cli = None
try:
    trading_cli = trading_client.TradingClient(API_KEY, API_SECRET, paper=True)
    try:
        data_cli = data_module.StockHistoricalDataClient(API_KEY, API_SECRET)
    except Exception:
        data_cli = None
    account = trading_cli.get_account()
    connected = True
except Exception as e:
    st.sidebar.error(f"Connection failed: {e}")
    connected = False

# --- Data Fetching Functions ---
def get_account():
    if not connected or not trading_cli: 
        return None
    try:
        return trading_cli.get_account()
    except:
        return None

def get_positions():
    if not connected or not trading_cli: 
        return []
    try:
        return trading_cli.get_all_positions()
    except:
        return []

def get_recent_orders(limit=10):
    if not connected or not trading_cli: 
        return []
    try:
        return trading_cli.get_orders(status="all", limit=limit)
    except:
        return []

def get_price_data():
    # Try Alpaca first
    if connected and data_cli:
        try:
            request = StockBarsRequest(
                symbol_or_symbols=[SYMBOL],
                timeframe=TimeFrame.Minute,
                start=datetime.utcnow() - timedelta(days=1),
                end=datetime.utcnow()
            )
            response = data_cli.get_stock_bars(request)
            if SYMBOL in response.data:
                df = response.data[SYMBOL]
                df.index = pd.to_datetime(df.index)
                return df
        except Exception:
            pass
    
    # Fallback to yfinance
    try:
        df = yf.download(SYMBOL, period="1d", interval="15m", progress=False)
        if df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except:
        return pd.DataFrame()

# --- UI Layout ---
st.title("🥇 Gold (GLD) Trading Bot Dashboard")

if not connected:
    st.warning("⚠️ Not connected to Alpaca. Using cached/demo data.")
else:
    st.success("✅ Connected to Alpaca Paper Trading")

# Sidebar Controls
st.sidebar.header("Controls")
bot_running = get_bot_state()
new_state = st.sidebar.toggle("Bot Running", value=bot_running, key="bot_toggle")
if new_state != bot_running:
    set_bot_state(new_state)
    st.sidebar.success(f"Bot {'started' if new_state else 'stopped'}")
auto_refresh = st.sidebar.toggle("Auto-Refresh (30s)", value=True)
if st.sidebar.button("Refresh Now"):
    st.rerun()

# Main Metrics
col1, col2, col3, col4 = st.columns(4)
account = get_account()
if account:
    col1.metric("Equity", f"${float(account.equity):,.2f}")
    col2.metric("Cash", f"${float(account.cash):,.2f}")
    col3.metric("Buying Power", f"${float(account.buying_power):,.2f}")
    col4.metric("Status", str(account.status))
else:
    col1.metric("Equity", "$100,000.00")
    col2.metric("Cash", "$100,000.00")
    col3.metric("Buying Power", "$200,000.00")
    col4.metric("Status", "Demo")

# Positions Table
st.subheader("Open Positions")
positions = get_positions()
if positions:
    pos_data = []
    for p in positions:
        pos_data.append({
            "Symbol": p.symbol, 
            "Qty": float(p.qty), 
            "Avg Price": float(p.avg_entry_price), 
            "Market Value": float(p.market_value), 
            "Unrealized P&L": float(p.unrealized_pl),
            "P&L %": float(p.unrealized_plpc) * 100
        })
    st.dataframe(pos_data, use_container_width=True)
else:
    st.info("No open positions.")

# Recent Orders
st.subheader("Recent Orders")
orders = get_recent_orders()
if orders:
    order_data = []
    for o in orders:
        order_data.append({
            "ID": str(o.id)[:8] + "...", 
            "Symbol": o.symbol,
            "Side": str(o.side), 
            "Qty": o.qty, 
            "Status": str(o.status), 
            "Submitted": str(o.submitted_at)[:19] if o.submitted_at else ""
        })
    st.dataframe(order_data, use_container_width=True)
else:
    st.info("No recent orders.")

# Price Chart
st.subheader(f"Live Price ({SYMBOL}) - Last 24h")
price_df = get_price_data()
if not price_df.empty and 'Close' in price_df.columns:
    st.line_chart(price_df['Close'])
elif not price_df.empty and 'close' in price_df.columns:
    st.line_chart(price_df['close'])
else:
    st.info("No price data available.")

# Bot Info
st.subheader("Bot Configuration")
st.json({
    "Strategy": "SMA Crossover (12/26) + RSI (14)",
    "Timeframe": "15 min (yfinance fallback)",
    "Symbol": SYMBOL,
    "Position Size": "10% of equity",
    "Stop Loss": "2% (disabled for fractional)",
    "Take Profit": "4% (disabled for fractional)",
    "Mode": "Paper Trading" if connected else "Demo",
    "Deployment": "Single Web Service (in-thread)"
})
st.caption("Bot runs in background thread alongside dashboard. Toggle 'Bot Running' in sidebar to pause/resume.")

# Auto-Refresh Logic
if auto_refresh and connected:
    import time
    time.sleep(30)
    st.rerun()
