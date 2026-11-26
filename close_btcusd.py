import sys
import time
sys.path.insert(0, r"C:\Users\USER\VScode prj\Trade")

from gold_trader import GoldTrader
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

# Create a trader for BTC/USD to check position
trader = GoldTrader(paper=True, symbol="BTC/USD")

# Check for open position
position = trader._position_for_symbol()

if position is None:
    print("No open BTC/USD position found.")
else:
    print(f"Open BTC/USD position found:")
    print(f"  Symbol: {position.symbol}")
    print(f"  Side: {position.side}")
    print(f"  Qty: {position.qty}")
    print(f"  Avg Entry Price: {position.avg_entry_price}")
    print(f"  Market Value: {position.market_value}")
    print(f"  Unrealized P&L: {position.unrealized_pl}")

    # For crypto, submit a MARKET order (SELL for long, BUY for short)
    # rather than using close_position() which doesn't work for crypto
    print("\nAttempting to close position via market order...")
    try:
        position_side = str(position.side).split(".")[-1].lower()
        close_side = OrderSide.SELL if position_side == "long" else OrderSide.BUY

        close_order = trader.trading_client.submit_order(
            MarketOrderRequest(
                symbol=trader.symbol,
                qty=float(position.qty),
                side=close_side,
                type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
            )
        )
        close_order_id = getattr(close_order, "id", None)
        print(f"Market order submitted. Order ID: {close_order_id}")
        print(f"  Side: {close_side}, Qty: {position.qty}")

        # Wait and check status
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            order = trader.trading_client.get_order_by_id(close_order_id)
            status = str(order.status).split(".")[-1].lower()
            print(f"  Order status: {status}")

            # Check if position is still open
            positions = trader.trading_client.get_all_positions()
            position_still_open = any(
                trader._position_matches(pos) for pos in positions
            )

            if status == "filled" and not position_still_open:
                print("Position successfully closed!")
                break
            if status in {"canceled", "rejected", "expired"}:
                print(f"Order ended with status: {status}")
                break
            time.sleep(1)
        else:
            print("Timed out waiting for order to fill.")
    except Exception as e:
        print(f"Failed to close position: {e}")
        import traceback
        traceback.print_exc()