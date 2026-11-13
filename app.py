"""
Llama Heatmap
-------------
A Flask app that polls an AI server's health and token counters every 60
seconds, stores each minute in a pluggable store (SQLite by default), and
renders a monthly calendar of per-hour status + throughput on a webpage
that auto-refreshes every 59 seconds.

The engine and the database are swappable instantiations: this file only
talks to an EngineSource and a Store (see the source/, store/ and core/
packages). Swapping llama.cpp for another engine, or SQLite for another
backend, means adding one file each — nothing here changes.

Usage:
    export LLAMA_SERVER_URL=http://127.0.0.1:8080   # default
    python app.py                                   # -> http://localhost:5000
"""

import calendar
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request

from core.deltas import minute_deltas
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


# --------------------------------------------------------------- api -------
@app.route("/")
def index():
    return render_template(
        "index.html",
        server_url=LLAMA_SERVER_URL,
        timezone=TIMEZONE,
        poll_interval=POLL_INTERVAL,
    )


@app.route("/api/status")
def api_status():
    """Latest health sample + live server details (source.details). Slot state
    is SYNTHESIZED from the last poller scrape's busy flag — never call /slots
    here: this endpoint is hit by the page's 59s auto-refresh, and /slots
    posts a task to llama.cpp, which resets --sleep-idle-seconds (and wakes a
    sleeping server). A <=60s-stale state is fine for a monitor."""
    row = store.latest_sample()
    status, ts, latency = ("unknown", None, None) if row is None else (row["status"], row["ts"], row["latency"])

    details = source.details()

    slots = None
    if details and details.get("total_slots"):
        if details.get("is_sleeping"):
            state = "asleep"
        else:
            busy = store.latest_busy()
            state = "processing" if busy else "idle"
        slots = [{"id": i, "state": state} for i in range(details["total_slots"])]

    return jsonify({
        "status": status,
        "ts": ts,
        "latency": latency,
        "server_url": LLAMA_SERVER_URL,
        "tz": TIMEZONE,
        "details": details,
        "slots": slots,
    })


@app.route("/api/calendar")
def api_calendar():
    """
    Per-hour status for a month (24 cells per day).
    Returns: { year, month, tz,
               cells:    { 'YYYY-MM-DD': ["up"/"down"/"sleep"/null, x24] },
               prompt_h: { 'YYYY-MM-DD': [int|null, x24] }  prefill tokens per hour,
               gen_h:    { 'YYYY-MM-DD': [int|null, x24] }  generated tokens per hour,
               day_totals, month_totals }
    A cell is 'sleep' when EVERY sample in the hour was asleep, 'up' when any
    sample was up (token shading then applies), 'down' when the hour was totally
    dead (no up, no sleep), null if no samples at all.
    """
    year = request.args.get("year", type=int) or datetime.now(TZ).year
    month = request.args.get("month", type=int) or datetime.now(TZ).month
    if not 1 <= month <= 12:
        month = datetime.now(TZ).month

    first_day = datetime(year, month, 1, tzinfo=TZ)
    last_day = datetime(year, month, calendar.monthrange(year, month)[1], 23, 59, 59, tzinfo=TZ)

    rows = store.samples_range(int(first_day.timestamp()), int(last_day.timestamp()))

    # bucket by (date_str, hour)
    buckets: dict[tuple[str, int], list[str]] = {}
    for r in rows:
        dt = datetime.fromtimestamp(r["ts"], TZ)
        buckets.setdefault((dt.strftime("%Y-%m-%d"), dt.hour), []).append(r["status"])

    cells: dict[str, list[str | None]] = {}
    for day in range(calendar.monthrange(year, month)[1]):
        d = first_day + timedelta(days=day)
        key = d.strftime("%Y-%m-%d")
        day_cells = []
        for h in range(24):
            samples = buckets.get((key, h))
            if not samples:
                day_cells.append(None)
                continue
            # red only when the hour was TOTALLY dead (zero up-samples);
            # a partial outage still shows the token shading of the recovered part.
            # amber when the server was asleep the entire hour (behaves like 'down':
            # one solid color, no token halves).
            ups = sum(1 for s in samples if s == "up")
            if ups > 0:
                day_cells.append("up")
            elif all(s == "sleep" for s in samples):
                day_cells.append("sleep")
            else:
                day_cells.append("down")
        cells[key] = day_cells

    # per-minute token sums (prefill / generated) + day & month totals
    mrows = store.metrics_range(int(first_day.timestamp()) - 3600, int(last_day.timestamp()))

    p_dm = minute_deltas(mrows, "prompt_total", int(first_day.timestamp()), int(last_day.timestamp()))
    g_dm = minute_deltas(mrows, "predict_total", int(first_day.timestamp()), int(last_day.timestamp()))
    c_dm = minute_deltas(mrows, "cached_total", int(first_day.timestamp()), int(last_day.timestamp()))

    # TRUE statistics: every real minute counts. On the DST fall-back day the
    # repeated 1 AM hour contributes TWICE to hour/day/month totals (real work
    # is real work). Only the 60 display cells can't show both occurrences.
    def by_day_h(dm):
        grouped: dict[str, list[tuple[int, int]]] = {}
        for ts, v in dm.items():
            dtm = datetime.fromtimestamp(ts, TZ)
            grouped.setdefault(dtm.strftime("%Y-%m-%d"), []).append((dtm.hour * 60 + dtm.minute, v))
        return grouped

    def by_day_total(dm):
        out: dict[str, int] = {}
        for ts, v in dm.items():
            d = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")
            out[d] = out.get(d, 0) + v
        return out

    p_grp, g_grp = by_day_h(p_dm), by_day_h(g_dm)
    c_tot = by_day_total(c_dm)
    prompt_h: dict[str, list[int | None]] = {}
    gen_h: dict[str, list[int | None]] = {}
    day_totals: dict[str, dict[str, int]] = {}
    month_prompt = month_gen = 0
    for day in range(calendar.monthrange(year, month)[1]):
        key = (first_day + timedelta(days=day)).strftime("%Y-%m-%d")
        ps, gs = [None] * 24, [None] * 24
        for minute, v in p_grp.get(key, []):
            ps[minute // 60] = (ps[minute // 60] or 0) + v
        for minute, v in g_grp.get(key, []):
            gs[minute // 60] = (gs[minute // 60] or 0) + v
        dp, dg = sum(v or 0 for v in ps), sum(v or 0 for v in gs)
        prompt_h[key], gen_h[key], day_totals[key] = ps, gs, {"prompt": dp, "gen": dg, "cached": c_tot.get(key, 0)}
        month_prompt, month_gen = month_prompt + dp, month_gen + dg
    month_cached = sum(c_tot.get((first_day + timedelta(days=d)).strftime("%Y-%m-%d"), 0)
                       for d in range(calendar.monthrange(year, month)[1]))

    return jsonify({
        "year": year, "month": month, "tz": TIMEZONE, "cells": cells,
        "prompt_h": prompt_h, "gen_h": gen_h,
        "day_totals": day_totals,
        "month_totals": {"prompt": month_prompt, "gen": month_gen, "cached": month_cached},
    })


@app.route("/api/day")
def api_day():
    """
    Per-minute status for one day (1440 cells, Pacific time).
    ?date=YYYY-MM-DD (default: today)
    """
    date_str = request.args.get("date") or datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=TZ)
    except ValueError:
        return jsonify({"error": "bad date, use YYYY-MM-DD"}), 400

    # TRUE-FRAME day: the strip covers the REAL elapsed minutes of the local
    # date — 1380 (spring-forward), 1440 (normal) or 1500 (fall-back). Index =
    # minutes after local midnight. On the fall-back day the repeated 1 AM
    # hour appears twice (blocks "01" and "01′"); on the spring-forward day
    # the skipped hour simply has no block. Totals equal the sum of the
    # visible cells in all cases.
    day_start = int(day.timestamp())  # local midnight, fold=0 (never ambiguous)
    day_end = int((day + timedelta(days=1)).timestamp())  # next local midnight
    n_min = (day_end - day_start) // 60

    rows = store.samples_range(day_start, day_end, hi_inclusive=False)

    by_off = {}
    for r in rows:
        by_off[(r["ts"] - day_start) // 60] = r["status"]
    cells = [by_off.get(i) for i in range(n_min)]

    # per-minute token deltas from cumulative /metrics counters.
    # Restart-safe: only diff consecutive rows with the same proc_start;
    # on a proc_start change record the counter value itself (tokens since boot).
    rows = store.metrics_range(day_start - 3600, day_end, hi_inclusive=False)

    def offset_map(dm: dict[int, int]) -> list:
        out = [None] * n_min
        for ts, v in dm.items():
            idx = (ts - day_start) // 60
            if 0 <= idx < n_min:
                out[idx] = v
        return out

    prompt_min = offset_map(minute_deltas(rows, "prompt_total", day_start, day_end - 1))
    cached_min = offset_map(minute_deltas(rows, "cached_total", day_start, day_end - 1))
    gen_min = offset_map(minute_deltas(rows, "predict_total", day_start, day_end - 1))

    # Note: a wake/reload minute (model coming back to VRAM, 0 tokens) is
    # deliberately NOT marked busy — purple means in-flight INFERENCING only;
    # recovery shows as plain idle grey.
    busy_min = [None] * n_min
    for r in rows:
        idx = (r["ts"] - day_start) // 60
        if 0 <= idx < n_min:
            busy_min[idx] = r["busy"]

    # hour-block labels: the wall-clock hour at each real-time block; a
    # repeated hour (fall-back) gets a prime, a skipped hour (spring) no block.
    hour_labels, prev_h = [], None
    for i in range(n_min // 60):
        dt = datetime.fromtimestamp(day_start + i * 3600, TZ)
        hour_labels.append(f"{dt.hour:02d}" + ("\u2032" if dt.hour == prev_h else ""))
        prev_h = dt.hour

    return jsonify({
        "date": date_str, "tz": TIMEZONE,
        "day_start": day_start, "n_min": n_min, "hour_labels": hour_labels,
        "cells": cells,
        "prompt_min": prompt_min,
        "cached_min": cached_min,
        "gen_min": gen_min,
        "busy_min": busy_min,
        "totals": {
            "prompt": sum(v or 0 for v in prompt_min),
            "cached": sum(v or 0 for v in cached_min),
            "gen": sum(v or 0 for v in gen_min),
        },
    })


# ----------------------------------------------------------------- main ----
def main():
    store.init()
    threading.Thread(target=poller_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "5000"))
    print(f"Llama heatmap: http://localhost:{port}  (watching {LLAMA_SERVER_URL}, every {POLL_INTERVAL}s)")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
