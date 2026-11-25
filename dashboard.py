import streamlit as st
import pandas as pd
import alpaca.trading.client as trading_client
import alpaca.data.historical as data_module
from alpaca.data.requests import StockBarsRequest
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from datetime import datetime, timedelta
import os
import json
import yfinance as yf
import threading
import time
from dotenv import load_dotenv

from gold_trader import (
    GoldTrader,
    HEARTBEAT_FILE,
    get_friday_close_symbols,
    get_trading_symbols,
    is_crypto_symbol,
)
from bot_state import get_bot_state, set_bot_state
from trade_history import build_trades, fetch_orders, open_position_rows, summarize_trades

load_dotenv()

SYMBOLS = get_trading_symbols()
ALLOCATION_FRACTION = 1 / len(SYMBOLS)
HAS_CRYPTO = any(is_crypto_symbol(symbol) for symbol in SYMBOLS)

# Page Config
st.set_page_config(
    page_title=f"{', '.join(SYMBOLS)} Trading Dashboard",
    layout="wide",
    page_icon="₿" if len(SYMBOLS) == 1 and HAS_CRYPTO else "📈",
)

st.markdown(
    """
    <style>
    :root {
        --gold: #d7ad58;
        --gold-bright: #f0cb78;
        --ink: #101714;
        --panel: #18211d;
        --panel-raised: #1d2923;
        --muted: #9aa99f;
        --line: rgba(215, 173, 88, 0.18);
        --green: #67d49a;
        --red: #ff8585;
        --text-color: #edf2ed;
        --background-color: #101714;
        --secondary-background-color: #18211d;
        --primary-color: #d7ad58;
    }

    .stApp {
        background:
            radial-gradient(ellipse at 82% 0%, rgba(108, 91, 48, 0.16), transparent 34rem),
            linear-gradient(145deg, #101714 0%, #111a16 48%, #101512 100%);
        color: #edf2ed;
    }
    [data-testid="stHeader"] { background: rgba(16, 23, 20, 0.72); }
    .stMainBlockContainer,
    [data-testid="stMain"] .block-container,
    [data-testid="stAppViewContainer"] .main .block-container {
        max-width: 1500px;
        padding: 2rem 2.5rem 4rem;
    }
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #151e19 0%, #111814 100%);
        border-right: 1px solid var(--line);
    }
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
        color: var(--muted);
    }
    h1, h2, h3 { color: #f1f3ec !important; letter-spacing: -0.035em; }
    h2 { font-size: 1.35rem !important; margin-top: 1.3rem !important; }
    [data-testid="stMarkdownContainer"] p,
    [data-testid="stMarkdownContainer"] li,
    [data-testid="stWidgetLabel"] p { color: #dce5dd; }
    [data-testid="stCaptionContainer"], small { color: var(--muted) !important; }

    .hero {
        position: relative;
        overflow: hidden;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 1.5rem;
        margin: 0.4rem 0 1.8rem;
        padding: 1.7rem 2rem;
        border: 1px solid var(--line);
        border-radius: 22px;
        background:
            radial-gradient(ellipse at 90% 10%, rgba(215,173,88,.19), transparent 34%),
            linear-gradient(115deg, rgba(29,41,35,.98), rgba(19,28,23,.94));
        box-shadow: 0 18px 50px rgba(0,0,0,.18);
    }
    .hero-kicker {
        color: var(--gold-bright);
        font-size: .72rem;
        font-weight: 700;
        letter-spacing: .18em;
        text-transform: uppercase;
        margin-bottom: .45rem;
    }
    .hero-title {
        color: #f4f2e9;
        font-size: clamp(1.7rem, 3vw, 2.5rem);
        font-weight: 700;
        letter-spacing: -.055em;
        line-height: 1.08;
    }
    .hero-copy { color: #aab8ae; margin-top: .55rem; font-size: .94rem; }
    .hero-right { display: flex; flex-direction: column; align-items: flex-end; gap: .55rem; }
    .status-pill {
        display: inline-flex;
        align-items: center;
        gap: .5rem;
        padding: .5rem .85rem;
        border: 1px solid rgba(103,212,154,.25);
        border-radius: 999px;
        color: #a5e8c1;
        background: rgba(103,212,154,.09);
        font-size: .78rem;
        font-weight: 650;
        white-space: nowrap;
    }
    .status-pill.offline {
        border-color: rgba(255,133,133,.25);
        color: #ffb0a7;
        background: rgba(255,133,133,.08);
    }
    .status-dot {
        width: .48rem;
        height: .48rem;
        border-radius: 50%;
        background: var(--green);
        box-shadow: 0 0 12px rgba(103,212,154,.55);
    }
    .status-pill.offline .status-dot {
        background: var(--red);
        box-shadow: 0 0 12px rgba(255,133,133,.45);
    }
    .hero-mode { color: var(--muted); font-size: .75rem; }

    [data-testid="stMetric"] {
        min-height: 112px;
        padding: 1rem 1.1rem;
        border: 1px solid rgba(255,255,255,.07);
        border-radius: 16px;
        background: linear-gradient(145deg, rgba(29,41,35,.94), rgba(23,32,27,.94));
        box-shadow: 0 8px 22px rgba(0,0,0,.12);
    }
    [data-testid="stMetricLabel"] p {
        color: #9eada3 !important;
        font-size: .78rem !important;
        font-weight: 600 !important;
    }
    [data-testid="stMetricValue"] p {
        color: #f0cb78 !important;
        font-size: 1.55rem !important;
        font-weight: 680 !important;
    }
    [data-testid="stMetricDelta"] p { font-size: .78rem !important; }
    [data-testid="stDataFrame"], [data-testid="stTable"] {
        border: 1px solid rgba(255,255,255,.08);
        border-radius: 14px;
        overflow: hidden;
        background: rgba(24,33,29,.7);
    }
    [data-testid="stAlert"] { border-radius: 13px; }
    [data-testid="stBaseButton-secondary"], [data-testid="stFormSubmitButton"] button {
        border: 1px solid rgba(215,173,88,.42) !important;
        border-radius: 10px !important;
        color: #f0cb78 !important;
        background: rgba(215,173,88,.08) !important;
        transition: all .18s ease;
    }
    [data-testid="stBaseButton-secondary"]:hover {
        border-color: var(--gold-bright) !important;
        background: rgba(215,173,88,.16) !important;
        transform: translateY(-1px);
    }
    [data-testid="stToggle"] label p { color: #e2e8e2; font-weight: 550; }
    [data-testid="stPlotlyChart"], [data-testid="stVegaLiteChart"] {
        padding: .5rem;
        border: 1px solid rgba(255,255,255,.07);
        border-radius: 16px;
        background: rgba(24,33,29,.62);
    }
    hr { border-color: rgba(255,255,255,.08) !important; }

    /* --- Mobile / small tablet --- */
    @media (max-width: 700px) {
        .stMainBlockContainer,
        [data-testid="stMain"] .block-container,
        [data-testid="stAppViewContainer"] .main .block-container {
            max-width: 100%;
            padding: 3.6rem .85rem 2.5rem !important;
        }
        [data-testid="stHeader"] { height: 2.85rem; }

        .hero {
            align-items: flex-start;
            flex-direction: column;
            gap: .9rem;
            margin: .25rem 0 1.1rem;
            padding: 1.15rem 1.2rem;
            border-radius: 16px;
        }
        .hero-title { font-size: 1.55rem; line-height: 1.12; }
        .hero-copy { font-size: .86rem; }
        .hero-right { align-items: flex-start; gap: .4rem; }
        .status-pill { padding: .4rem .7rem; font-size: .72rem; }

        h2 { font-size: 1.15rem !important; margin-top: 1rem !important; }
        h3, h4 { font-size: 1rem !important; }

        /* Stack metric rows two-up instead of one long column */
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) {
            display: grid !important;
            grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
            gap: .55rem !important;
        }
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) > [data-testid="stColumn"] {
            width: auto !important;
            flex: 1 1 auto !important;
            min-width: 0 !important;
        }
        [data-testid="stMetric"] {
            min-height: 0;
            padding: .7rem .8rem;
            border-radius: 12px;
        }
        [data-testid="stMetricLabel"] p { font-size: .7rem !important; }
        [data-testid="stMetricValue"] p { font-size: 1.15rem !important; }
        [data-testid="stMetricDelta"] p { font-size: .7rem !important; }

        /* Full-width tap targets for controls */
        [data-testid="stBaseButton-secondary"],
        [data-testid="stFormSubmitButton"] button,
        [data-testid="stSelectbox"] { width: 100% !important; }
        [data-testid="stToggle"] { padding: .15rem 0; }

        /* Tables stay scrollable rather than squashed */
        [data-testid="stDataFrame"], [data-testid="stTable"] {
            border-radius: 10px;
        }
        [data-testid="stDataFrame"] { max-width: 100%; overflow-x: auto; }
        [data-testid="stPlotlyChart"], [data-testid="stVegaLiteChart"] {
            padding: .25rem;
            border-radius: 12px;
        }
        [data-testid="stAlert"] { font-size: .84rem; }
        [data-testid="stJson"] { font-size: .72rem; }
        [data-testid="stJson"] div,
        [data-testid="stJson"] span {
            font-size: .75rem !important;
            overflow-wrap: break-word !important;
            word-break: normal !important;
        }
    }

    @media (max-width: 390px) {
        .stMainBlockContainer,
        [data-testid="stMain"] .block-container,
        [data-testid="stAppViewContainer"] .main .block-container { padding: 3.5rem .6rem 2rem !important; }
        [data-testid="stMetricValue"] p { font-size: 1rem !important; }
        .hero-title { font-size: 1.35rem; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Start one strategy worker per configured symbol.
@st.cache_resource
def start_bot_threads():
    def run_bot(symbol):
        try:
            trader = GoldTrader(
                paper=True,
                symbol=symbol,
                allocation_fraction=ALLOCATION_FRACTION,
            )
            trader.run(loop=True, interval=60)
        except Exception as e:
            print(f"Bot thread error for {symbol}: {e}")

    threads = [
        threading.Thread(
            target=run_bot,
            args=(symbol,),
            name=f"trader-{symbol.replace('/', '-')}",
            daemon=True,
        )
        for symbol in SYMBOLS
    ]
    for thread in threads:
        thread.start()
    return threads

# Initialize bot threads only when the dashboard owns them. When the service is
# booted through start.py the workers are already running and must not be
# duplicated.
if os.getenv("BOT_MANAGED_EXTERNALLY") != "1":
    start_bot_threads()

# --- Config ---
API_KEY = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_API_SECRET")

# Try to connect
connected = False
trading_cli = None
data_cli = None
crypto_data_cli = None
try:
    trading_cli = trading_client.TradingClient(API_KEY, API_SECRET, paper=True)
    if HAS_CRYPTO:
        try:
            from alpaca.data.historical import CryptoHistoricalDataClient
            crypto_data_cli = CryptoHistoricalDataClient(API_KEY, API_SECRET)
        except Exception as e:
            st.sidebar.warning(f"Crypto data unavailable; Yahoo Finance fallback will be used: {e}")
    if any(not is_crypto_symbol(symbol) for symbol in SYMBOLS):
        try:
            data_cli = data_module.StockHistoricalDataClient(API_KEY, API_SECRET)
        except Exception as e:
            st.sidebar.warning(f"Stock data unavailable; Yahoo Finance fallback will be used: {e}")
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

def load_heartbeats():
    try:
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}

def pnl_color(value):
    if value < 0:
        return "color: #ff8585; font-weight: 600"
    if value > 0:
        return "color: #67d49a; font-weight: 600"
    return "color: #dce5dd"

def get_price_data(symbol):
    # Try Alpaca first
    if connected and is_crypto_symbol(symbol) and crypto_data_cli:
        try:
            request = CryptoBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                start=datetime.utcnow() - timedelta(days=1),
                end=datetime.utcnow()
            )
            response = crypto_data_cli.get_crypto_bars(request)
            bars = response.data.get(symbol, [])
            if bars:
                df = pd.DataFrame(
                    [bar.model_dump(exclude={"symbol"}) for bar in bars]
                ).set_index("timestamp")
                df.index = pd.to_datetime(df.index)
                df.columns = [str(column).title() for column in df.columns]
                return df
        except Exception:
            pass

    if connected and not is_crypto_symbol(symbol) and data_cli:
        try:
            request = StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame.Minute,
                start=datetime.utcnow() - timedelta(days=1),
                end=datetime.utcnow()
            )
            response = data_cli.get_stock_bars(request)
            if symbol in response.data:
                df = response.data[symbol]
                df.index = pd.to_datetime(df.index)
                return df
        except Exception:
            pass
    
    # Fallback to yfinance
    try:
        ticker = "BTC-USD" if is_crypto_symbol(symbol) else symbol
        df = yf.download(ticker, period="1d", interval="15m", progress=False)
        if df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except:
        return pd.DataFrame()

# --- UI Layout ---
connection_class = "" if connected else "offline"
connection_label = "Alpaca connected" if connected else "Alpaca unavailable"
mode_label = "PAPER TRADING" if connected else "DEMO MODE"
st.markdown(
    f"""
    <div class="hero">
      <div>
        <div class="hero-kicker">Multi-asset · automated strategy</div>
        <div class="hero-title">{", ".join(SYMBOLS)} Trading Desk</div>
        <div class="hero-copy">EMA 50/200 trend · RSI thresholds · 15-minute bars per asset</div>
      </div>
      <div class="hero-right">
        <div class="status-pill {connection_class}">
          <span class="status-dot"></span>{connection_label}
        </div>
        <div class="hero-mode">{mode_label}</div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

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
st.subheader("Account Overview")
col1, col2, col3, col4 = st.columns(4)
account = get_account()
if account:
    account_status = str(account.status).split(".")[-1].replace("_", " ").title()
    col1.metric("Equity", f"${float(account.equity):,.2f}")
    col2.metric("Cash", f"${float(account.cash):,.2f}")
    col3.metric("Buying Power", f"${float(account.buying_power):,.2f}")
    col4.metric("Status", account_status)
else:
    col1.metric("Equity", "$100,000.00")
    col2.metric("Cash", "$100,000.00")
    col3.metric("Buying Power", "$200,000.00")
    col4.metric("Status", "Demo")

# Worker heartbeat: proves the strategy threads are alive even when nobody is
# trading, and makes a dormant bot obvious instead of guessing from order flow.
st.markdown("---")
st.subheader("Bot Heartbeat")
heartbeats = load_heartbeats()
if not heartbeats:
    st.warning(
        "No worker heartbeat found. The strategy workers are probably not running "
        "(start the service with start.py so they boot without a browser session)."
    )
else:
    now_utc = pd.Timestamp.now(tz="UTC")
    heartbeat_rows = []
    for symbol in SYMBOLS:
        beat = heartbeats.get(symbol) or {}
        last_loop = pd.to_datetime(beat.get("last_loop"), utc=True, errors="coerce")
        age_seconds = (now_utc - last_loop).total_seconds() if pd.notna(last_loop) else None
        status = str(beat.get("status", "missing")).upper()
        if age_seconds is not None and age_seconds > 180 and status != "PAUSED":
            status = f"{status} (STALE)"
        heartbeat_rows.append(
            {
                "Symbol": symbol,
                "Status": status,
                "Heartbeat age": f"{int(age_seconds)}s" if age_seconds is not None else "n/a",
                "Last processed bar": beat.get("last_bar", "n/a"),
                "Last signal": beat.get("last_signal", "n/a"),
                "Last error": beat.get("last_error") or "",
            }
        )
    st.table(pd.DataFrame(heartbeat_rows).set_index("Symbol"))

# Positions Table
st.markdown("---")
st.subheader("Portfolio")
st.markdown("#### Open positions")
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
    positions_df = pd.DataFrame(pos_data)

    styled_positions = positions_df.style.map(
        pnl_color, subset=["Unrealized P&L", "P&L %"]
    ).format({
        "Qty": "{:,.2f}",
        "Avg Price": "${:,.2f}",
        "Market Value": "${:,.2f}",
        "Unrealized P&L": "${:,.2f}",
        "P&L %": "{:+.2f}%",
    })
    st.dataframe(styled_positions, use_container_width=True)
else:
    st.info("No open positions.")

# Trade P&L
st.markdown("---")
st.subheader("Trade P&L")
closed_trades = []
history_error = None
if connected and trading_cli:
    try:
        closed_trades = build_trades(fetch_orders(trading_cli))
    except Exception as e:
        history_error = e

if history_error is not None:
    st.warning(f"Could not load trade history: {history_error}")

trade_rows = closed_trades + open_position_rows(positions)
if trade_rows:
    trade_summary = summarize_trades(trade_rows)
    avg_win = trade_summary["avg_win"]
    avg_loss = trade_summary["avg_loss"]
    avg_win_label = f"${avg_win:,.0f}" if avg_win is not None else "—"
    avg_loss_label = f"-${abs(avg_loss):,.0f}" if avg_loss is not None else "—"
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Realized P&L", f"${trade_summary['realized_pnl']:+,.2f}")
    p2.metric("Closed trades", trade_summary["closed_count"])
    p3.metric(
        "Win rate",
        f"{trade_summary['win_rate']:.0f}%"
        if trade_summary["win_rate"] is not None
        else "—",
    )
    p4.metric("Avg win / loss", f"{avg_win_label} / {avg_loss_label}")
    trade_data = [
        {
            "Symbol": row["symbol"],
            "Side": row["side"],
            "Qty": row["qty"],
            "Entry": row["entry_time"].strftime("%Y-%m-%d %H:%M") if row["entry_time"] else None,
            "Entry Price": row["entry_price"],
            "Exit / Mark": row["exit_time"].strftime("%Y-%m-%d %H:%M") if row["exit_time"] else None,
            "Exit Price": row["exit_price"],
            "P&L": row["pnl"],
            "P&L %": row["pnl_pct"],
            "Status": row["status"],
        }
        for row in trade_rows
    ]
    trade_display = pd.DataFrame(trade_data)
    styled_trades = trade_display.style.map(
        pnl_color, subset=["P&L", "P&L %"]
    ).format(
        {
            "Qty": "{:,.6g}",
            "Entry Price": "${:,.2f}",
            "Exit Price": "${:,.2f}",
            "P&L": "${:+,.2f}",
            "P&L %": "{:+.2f}%",
        },
        na_rep="—",
    )
    st.dataframe(styled_trades, use_container_width=True)
    st.caption(
        "Realized P&L is paired from Alpaca fills (FIFO) and excludes fees, which Alpaca's "
        "paper API does not report. Open rows show live unrealized P&L."
    )
else:
    st.info("No trades recorded yet.")

# Orders History
st.markdown("#### Recent orders")
try:
    if connected and trading_cli:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=50)
        all_orders = trading_cli.get_orders(filter=req)
        if all_orders:
            order_data = []
            for o in all_orders:
                order_data.append({
                    "ID": str(o.id)[:8] + "...",
                    "Symbol": o.symbol,
                    "Side": str(o.side).replace('OrderSide.', ''),
                    "Qty": getattr(o, 'qty', None),
                    "Type": str(o.type).replace('OrderType.', '') if hasattr(o, 'type') else '',
                    "Status": str(o.status).replace('OrderStatus.', ''),
                    "Submitted": str(getattr(o, 'submitted_at', ''))[:19]
                })
            st.dataframe(order_data, use_container_width=True)
        else:
            st.info("No orders found.")
    else:
        st.info("Not connected to Alpaca.")
except Exception as e:
    st.warning(f"Could not load orders: {e}")

# EMA/RSI Status
st.markdown("---")
st.subheader("Technical Indicators (EMA 50/200 + RSI/ATR 14)")
selected_symbol = st.selectbox("Market", SYMBOLS, key="market_selector")
selected_is_crypto = is_crypto_symbol(selected_symbol)
if selected_is_crypto:
    st.warning(
        "BTC/USD orders have no broker-side stop-loss or take-profit. "
        "BTC/USD spot trading is long-only; the bot exits with a market order "
        "when the strategy signals a reversal. Losses can exceed the 1% ATR-based "
        "sizing estimate while the bot waits for a signal or is offline."
    )
try:
    trader = GoldTrader(
        paper=True,
        symbol=selected_symbol,
        allocation_fraction=ALLOCATION_FRACTION,
    )
    df = trader._closed_bars(trader._get_data(limit=1000))
    if not df.empty:
        df_ind = trader._calculate_indicators(df)
        if len(df_ind) > 0:
            last = df_ind.iloc[-1]
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Close", f"${float(last['Close']):.2f}")
            c2.metric("EMA 50", f"${float(last['EMA_Fast']):.2f}" if pd.notna(last.get('EMA_Fast')) else "N/A")
            c3.metric("EMA 200", f"${float(last['EMA_Slow']):.2f}" if pd.notna(last.get('EMA_Slow')) else "N/A")
            c4.metric("RSI (14)", f"{float(last['RSI']):.2f}" if pd.notna(last.get('RSI')) else "N/A")
            c5.metric("ATR (14)", f"${float(last['ATR']):.2f}" if pd.notna(last.get('ATR')) else "N/A")
            st.caption(f"Data: {trader.last_data_source} · Completed bar: {df_ind.index[-1]}")
            if pd.notna(last.get('EMA_Fast')) and pd.notna(last.get('EMA_Slow')):
                if last['EMA_Fast'] > last['EMA_Slow']:
                    st.info("EMA 50 > EMA 200 (Bullish)")
                elif last['EMA_Fast'] < last['EMA_Slow']:
                    st.info("EMA 50 < EMA 200 (Bearish)")
                else:
                    st.info("EMA 50 == EMA 200")
        else:
            st.info("Not enough data to calculate indicators.")
    else:
        st.info("No data available for indicators.")
except Exception as e:
    st.warning(f"Could not load indicators: {e}")

# Price Chart
st.markdown("---")
st.subheader(f"Live Price ({selected_symbol}) - Last 24h")
price_df = get_price_data(selected_symbol)
if not price_df.empty and 'Close' in price_df.columns:
    st.line_chart(price_df['Close'])
elif not price_df.empty and 'close' in price_df.columns:
    st.line_chart(price_df['close'])
else:
    st.info("No price data available.")

# Bot Info
st.markdown("---")
st.subheader("Bot Configuration")
bot_configuration = {
    "Strategy": "EMA (50/200) trend + RSI (14) thresholds",
    "Entry": "RSI above 40 in bull trend; below 60 in bear trend",
    "Timeframe": "15-minute completed bars",
    "Symbols": ", ".join(SYMBOLS),
    "Position Size": (
        "1% equity risk target per asset; buying power is split evenly between configured assets. "
        "BTC/USD losses can exceed the estimate "
        "because it has no broker-side stop"
    ),
    "Buying Power Cap": f"Up to {100 * ALLOCATION_FRACTION:.2f}% per symbol",
    "Mode": "Paper Trading" if connected else "Demo",
    "Deployment": f"Single Web Service ({len(SYMBOLS)} in-process strategy workers)"
}
bot_configuration["Exits"] = (
    "BTC/USD: long-only, market close on opposing signal, no broker stop/target; "
    "other symbols: 1.5 x ATR stop and 3 x ATR target"
    if HAS_CRYPTO and any(not is_crypto_symbol(symbol) for symbol in SYMBOLS)
    else (
        "Long-only, market close on opposing signal, no broker stop/target"
        if HAS_CRYPTO else "1.5 x ATR stop and 3 x ATR target"
    )
)
friday_close_symbols = [symbol for symbol in get_friday_close_symbols() if symbol in SYMBOLS]
if friday_close_symbols:
    bot_configuration["Weekend Flatten"] = (
        f"{', '.join(friday_close_symbols)} is closed ~10 minutes before the close of the "
        f"last trading day of each week (Friday, or the preceding session when Friday is a "
        f"holiday) so no position is carried over the weekend. New entries are blocked "
        f"for the rest of that day."
    )
st.json(bot_configuration)
st.caption("Set TRADING_SYMBOLS=GLD,BTC/USD to trade both. TRADING_SYMBOL remains available for a single-symbol run.")
st.caption("The service starts one strategy worker per symbol at boot (start.py). Toggle 'Bot Running' to pause/resume all workers.")

# Auto-Refresh Logic
if auto_refresh and connected:
    import time
    time.sleep(30)
    st.rerun()
