from alpaca.trading.client import TradingClient
import os

api_key = os.getenv("ALPACA_API_KEY") or "PKPKXJ7PJ2GGVX2E65E3HBSBNN"
api_secret = os.getenv("ALPACA_API_SECRET") or "3t8e2S4XhgH399wgVEmog53FcMkzfi64FKdUBpvRtfNo"

try:
    client = TradingClient(api_key, api_secret, paper=True)
    account = client.get_account()
    print("connected", account.status, account.equity)
except Exception as e:
    print("err", e)
