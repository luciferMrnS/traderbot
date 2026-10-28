import threading
import os
import subprocess
import sys
from gold_trader import GoldTrader

def run_bot():
    try:
        trader = GoldTrader(paper=True, symbol="GLD")
        trader.run(loop=True, interval=60)
    except Exception as e:
        print(f"Bot error: {e}", file=sys.stderr)

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    port = os.environ.get("PORT", "8501")
    subprocess.run([
        sys.executable, "-m", "streamlit", "run", "dashboard.py",
        f"--server.port={port}",
        "--server.address=0.0.0.0",
        "--server.headless=true"
    ])
