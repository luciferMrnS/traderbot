from collections import deque
from datetime import datetime, timedelta, timezone

EPSILON = 1e-9


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _order_field(order, name):
    if isinstance(order, dict):
        return order.get(name)
    return getattr(order, name, None)


def _flatten_orders(orders):
    seen = set()
    flat = []
    stack = list(orders)
    while stack:
        order = stack.pop()
        order_id = str(_order_field(order, "id") or "")
        key = order_id or id(order)
        if key in seen:
            continue
        seen.add(key)
        flat.append(order)
        stack.extend(_order_field(order, "legs") or [])
    return flat


def _fill_from_order(order):
    qty = _as_float(_order_field(order, "filled_qty"))
    price = _as_float(_order_field(order, "filled_avg_price"))
    side = str(_order_field(order, "side") or "").split(".")[-1].lower()
    symbol = str(_order_field(order, "symbol") or "")
    if qty is None or price is None or qty <= 0 or price <= 0:
        return None
    if side not in ("buy", "sell") or not symbol:
        return None
    filled_at = _order_field(order, "filled_at") or _order_field(order, "submitted_at")
    if not isinstance(filled_at, datetime):
        return None
    if filled_at.tzinfo is None:
        filled_at = filled_at.replace(tzinfo=timezone.utc)
    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "time": filled_at,
    }


def _trades_from_fills(fills):
    trades = []
    open_lots = {}
    for fill in fills:
        direction = 1 if fill["side"] == "buy" else -1
        lots = open_lots.setdefault(fill["symbol"], deque())
        remaining = fill["qty"]
        while remaining > EPSILON and lots and lots[0]["direction"] != direction:
            lot = lots[0]
            matched = min(lot["qty"], remaining)
            entry_notional = lot["price"] * matched
            pnl = (fill["price"] - lot["price"]) * matched * lot["direction"]
            trades.append(
                {
                    "symbol": fill["symbol"],
                    "side": "long" if lot["direction"] > 0 else "short",
                    "qty": matched,
                    "entry_time": lot["time"],
                    "entry_price": lot["price"],
                    "exit_time": fill["time"],
                    "exit_price": fill["price"],
                    "pnl": pnl,
                    "pnl_pct": (pnl / entry_notional * 100) if entry_notional else None,
                    "status": "closed",
                }
            )
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] <= EPSILON:
                lots.popleft()
        if remaining > EPSILON:
            lots.append(
                {
                    "direction": direction,
                    "qty": remaining,
                    "price": fill["price"],
                    "time": fill["time"],
                }
            )
    return trades


def build_trades(orders):
    fills = [fill for fill in map(_fill_from_order, _flatten_orders(orders)) if fill]
    fills.sort(key=lambda fill: fill["time"])
    trades = _trades_from_fills(fills)
    trades.sort(key=lambda trade: trade["exit_time"], reverse=True)
    return trades


def open_position_rows(positions):
    rows = []
    for position in positions:
        qty = _as_float(getattr(position, "qty", None))
        entry_price = _as_float(getattr(position, "avg_entry_price", None))
        current_price = _as_float(getattr(position, "current_price", None))
        pnl_pct = _as_float(getattr(position, "unrealized_plpc", None))
        if qty is None or entry_price is None or current_price is None:
            continue
        rows.append(
            {
                "symbol": str(getattr(position, "symbol", "") or ""),
                "side": str(getattr(position, "side", "") or "long").split(".")[-1].lower(),
                "qty": qty,
                "entry_time": None,
                "entry_price": entry_price,
                "exit_time": None,
                "exit_price": current_price,
                "pnl": _as_float(getattr(position, "unrealized_pl", None)),
                "pnl_pct": pnl_pct * 100 if pnl_pct is not None else None,
                "status": "open",
            }
        )
    return rows


def summarize_trades(trades):
    closed = [trade for trade in trades if trade["status"] == "closed"]
    wins = [trade["pnl"] for trade in closed if trade["pnl"] > 0]
    losses = [trade["pnl"] for trade in closed if trade["pnl"] < 0]
    return {
        "closed_count": len(closed),
        "realized_pnl": sum(trade["pnl"] for trade in closed),
        "win_rate": (len(wins) / len(closed) * 100) if closed else None,
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
        "best": max((trade["pnl"] for trade in closed), default=None),
        "worst": min((trade["pnl"] for trade in closed), default=None),
    }


def fetch_orders(trading_client, page_size=500, max_pages=20):
    from alpaca.common.enums import Sort
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    collected = []
    seen = set()
    until = None
    for _ in range(max_pages):
        page = trading_client.get_orders(
            filter=GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                limit=page_size,
                nested=True,
                direction=Sort.DESC,
                until=until,
            )
        )
        if not page:
            break
        new_orders = 0
        for order in page:
            key = str(_order_field(order, "id") or "")
            if key and key in seen:
                continue
            seen.add(key)
            collected.append(order)
            new_orders += 1
        submitted = [
            _order_field(order, "submitted_at")
            for order in page
            if isinstance(_order_field(order, "submitted_at"), datetime)
        ]
        if new_orders == 0 or len(page) < page_size or not submitted:
            break
        until = min(submitted) - timedelta(microseconds=1)
    return collected
