"""
Llama Heatmap
-------------
A Flask app that polls an AI server's health and token counters every 60
seconds, stores each minute in a pluggable store (SQLite by default), and
renders a monthly calendar of per-hour status + throughput on a webpage
that auto-refreshes every 59 seconds.

The engine, the persistence and the API are swappable subpackages:

    source/  EngineSource protocol + LlamaCppSource (which server to watch)
    store/   Store protocol + SqliteStore (where minutes live)
    api/     the JSON contract the frontend depends on (one module per
             endpoint, wired as a Blueprint)
    core/    pure per-minute / DST aggregation logic, no I/O

This file is the composition root: config, instantiation, poller thread,
the dashboard page. Swapping llama.cpp for another engine, or SQLite for
another backend, means adding one file each — nothing here changes.

Usage:
    export LLAMA_SERVER_URL=http://127.0.0.1:8080   # default
    python app.py                                   # -> http://localhost:5000
"""

import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, render_template

from api import ApiContext, create_api
from source.llamacpp import LlamaCppSource
from store.sqlite import SqliteStore

# ---------------------------------------------------------------- config ---
LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
POLL_INTERVAL = 60                      # seconds between health checks
TIMEZONE = os.environ.get("MONITOR_TZ", "America/Los_Angeles")  # Pacific
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.db")
TZ = ZoneInfo(TIMEZONE)

# ------------------------------------------------------ instantiation -----
source = LlamaCppSource(LLAMA_SERVER_URL)   # the engine (sleep-safe endpoints only)
store = SqliteStore(DB_PATH)                # the persistence

app = Flask(__name__)
app.register_blueprint(create_api(ApiContext(
    source=source, store=store,
    server_url=LLAMA_SERVER_URL, tz_name=TIMEZONE, tz=TZ,
)))


# ------------------------------------------------------------- poller ------
def poller_loop() -> None:
    last_minute = -1
    while True:
        try:
            now = datetime.now(TZ).replace(second=0, microsecond=0)
            if int(now.timestamp()) != last_minute:
                ts = int(now.timestamp())
                status, latency = source.health()
                store.add_sample(ts, status, latency)
                snap = source.metrics() if status in ("up", "sleep") else None
                if snap:
                    store.add_metrics(ts, snap)
                last_minute = ts
                print(f"[{now:%Y-%m-%d %H:%M}] {LLAMA_SERVER_URL} -> {status.upper()}"
                      + (f" ({latency:.0f} ms)" if latency is not None else ""))
        except Exception as e:
            print(f"poller error: {e}")
        # sleep to the next minute boundary
        sleep_s = max(1.0, 60 - (time.monotonic() % 60))
        time.sleep(sleep_s)


# --------------------------------------------------------------- page ------
@app.route("/")
def index():
    return render_template(
        "index.html",
        server_url=LLAMA_SERVER_URL,
        timezone=TIMEZONE,
        poll_interval=POLL_INTERVAL,
    )


# ----------------------------------------------------------------- main ----
def main():
    store.init()
    threading.Thread(target=poller_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "5000"))
    print(f"Llama heatmap: http://localhost:{port}  (watching {LLAMA_SERVER_URL}, every {POLL_INTERVAL}s)")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
