"""Boot entrypoint for the deployed service.

The strategy workers must start when the process boots, not when a browser opens
the Streamlit dashboard. An HTTP-only uptime check keeps the container alive but
never executes the dashboard script, so starting the bot from dashboard.py left
it dormant after every restart until someone opened the page.

This wrapper starts the workers first, then serves the dashboard in a child
process on the port Render provides.
"""
import os
import subprocess
import sys
import threading
import time

from gold_trader import GoldTrader, get_trading_symbols


def run_worker(symbol, allocation_fraction):
    """Keep one strategy worker alive for the lifetime of the process."""
    while True:
        try:
            GoldTrader(
                paper=True,
                symbol=symbol,
                allocation_fraction=allocation_fraction,
            ).run(loop=True, interval=60)
        except Exception as exc:  # pragma: no cover - defensive supervisor
            print(
                f"Bot worker for {symbol} crashed: {exc}",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(10)


def main():
    symbols = get_trading_symbols()
    allocation_fraction = 1 / len(symbols)
    for symbol in symbols:
        threading.Thread(
            target=run_worker,
            args=(symbol, allocation_fraction),
            name=f"trader-{symbol.replace('/', '-')}",
            daemon=True,
        ).start()
        print(f"Started strategy worker for {symbol}", flush=True)

    # The dashboard must not start a second set of workers.
    os.environ["BOT_MANAGED_EXTERNALLY"] = "1"

    port = os.environ.get("PORT", "8501")
    base_directory = os.path.dirname(os.path.abspath(__file__))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            os.path.join(base_directory, "dashboard.py"),
            f"--server.port={port}",
            "--server.address=0.0.0.0",
            "--server.headless=true",
        ],
        cwd=base_directory,
        check=False,
    )


if __name__ == "__main__":
    main()
