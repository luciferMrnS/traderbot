import os
from gold_trader import get_trading_symbols, normalize_trading_symbol

# Show what the env vars look like
print("TRADING_SYMBOLS env var:", os.getenv("TRADING_SYMBOLS"))
print("TRADING_SYMBOL env var:", os.getenv("TRADING_SYMBOL"))

symbols = get_trading_symbols()
print("\nget_trading_symbols() returns:", symbols)

# Show normalization
for s in symbols:
    print(f"  '{s}' -> normalized: '{normalize_trading_symbol(s)}'")