"""
Read-only diagnostic for the GLD trading bot.

Replicates the bot's exact data -> indicators -> signal pipeline and
inspects the paper account (clock, positions, orders).
It does NOT submit any orders.
"""
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

import pandas as pd
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

from gold_trader import GoldTrader, STRATEGY_STATE_FILE


def main():
    t = GoldTrader(paper=True, symbol="GLD")

    print("\n=== 1. DATA PIPELINE (exactly as the bot sees it) ===")
    print(f"Alpaca connected={t.use_alpaca}  data_client={t.data_client is not None}")
    df = t._closed_bars(t._get_data(limit=1000))
    if df.empty:
        print("!! No completed 15-minute bars received")
        return
    print(f"data source: {t.last_data_source}")
    print(f"rows={len(df)}")
    print(f"first bar: {df.index[0]}")
    print(f"last closed bar: {df.index[-1]}")
    print(f"columns: {list(df.columns)}")
    print(f"last close: ${float(df.iloc[-1]['Close']):.2f}")

    ind = t._calculate_indicators(df)
    print("\n=== 2. INDICATORS (last 3 bars) ===")
    cols = [c for c in ['Close', 'EMA_Fast', 'EMA_Slow', 'RSI', 'ATR'] if c in ind.columns]
    with pd.option_context('display.width', 140):
        print(ind[cols].tail(3))

    print("\n=== 3. DECISION LOGIC ===")
    last = ind.iloc[-1]
    if pd.isna(last['EMA_Fast']) or pd.isna(last['EMA_Slow']):
        regime = None
    else:
        regime = 'bull' if last['EMA_Fast'] > last['EMA_Slow'] else ('bear' if last['EMA_Fast'] < last['EMA_Slow'] else 'neutral')
    print(f"current regime: {regime}")
    try:
        with open(STRATEGY_STATE_FILE, encoding="utf-8") as f:
            print(f"persisted strategy state: {f.read()}")
    except Exception as e:
        print(f"strategy state: not found ({e})")
    sig = t._generate_signal(ind)
    print(f"entry signal: {sig!r}")
    print("BUY: EMA50 > EMA200 and RSI crosses above 40; SELL: EMA50 < EMA200 and RSI crosses below 60")
    print("Stop: 1.5 x ATR(14); target: 3 x ATR(14); risk budget: 1% of equity")

    if not t.use_alpaca or not t.trading_client:
        print("\n(not connected to Alpaca; skipping account section)")
        return

    print("\n=== 4. ACCOUNT / MARKET STATE (read-only) ===")
    try:
        clock = t.trading_client.get_clock()
        print(f"market open: {clock.is_open}   now: {clock.timestamp}")
        print(f"next open: {clock.next_open}   next close: {clock.next_close}")
    except Exception as e:
        print(f"clock failed: {e}")

    try:
        acct = t.trading_client.get_account()
        print(f"equity: ${float(acct.equity):,.2f}   cash: ${float(acct.cash):,.2f}   status: {acct.status}")
    except Exception as e:
        print(f"account failed: {e}")

    try:
        positions = t.trading_client.get_all_positions()
        if not positions:
            print("positions: NONE (bot is flat)")
        for p in positions:
            print(f"POS: {p.symbol} qty={p.qty} side={p.side} avg_entry=${float(p.avg_entry_price):.2f} "
                  f"mkt_val=${float(p.market_value):,.2f} uPnL=${float(p.unrealized_pl):.2f}")
    except Exception as e:
        print(f"positions failed: {e}")

    try:
        orders = t.trading_client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.ALL, limit=25))
        if not orders:
            print("orders: NONE")
        for o in orders:
            otype = getattr(o, 'type', getattr(o, 'order_type', '?'))
            print(f"ORDER: {o.submitted_at}  {o.symbol} "
                  f"{str(o.side).replace('OrderSide.', '')} qty={o.qty} "
                  f"{str(otype).replace('OrderType.', '')} "
                  f"status={str(o.status).replace('OrderStatus.', '')}")
    except Exception as e:
        print(f"orders failed: {e}")


if __name__ == "__main__":
    main()
