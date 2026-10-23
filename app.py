"""
Llama Heatmap
-------------
A Flask app that polls a llama.cpp server's /health endpoint every 60 seconds,
stores each sample in SQLite, and renders a monthly calendar showing per-hour
status (up / down / no data) on a webpage that auto-refreshes every minute.

Usage:
    export LLAMA_SERVER_URL=http://127.0.0.1:8080   # default
    python app.py                                   # -> http://localhost:5000
"""

import calendar
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------- config ---
LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
POLL_INTERVAL = 60                      # seconds between health checks
TIMEZONE = os.environ.get("MONITOR_TZ", "America/Los_Angeles")  # Pacific
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.db")
TZ = ZoneInfo(TIMEZONE)

app = Flask(__name__)
_lock = threading.Lock()


# ----------------------------------------------------------------- db ------
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS samples (
                ts        INTEGER NOT NULL,   -- unix epoch seconds, truncated to minute
                status    TEXT    NOT NULL,   -- 'up' | 'down'
                latency   REAL,               -- ms, NULL when down
                PRIMARY KEY (ts)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples (ts)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metrics (
                ts             INTEGER PRIMARY KEY,  -- unix epoch seconds, truncated to minute
                proc_start     INTEGER,              -- Process-Start-Time-Unix (restart detector)
                prompt_total   INTEGER,              -- cumulative prompt tokens (excl. cache)
                cached_total   INTEGER,              -- cumulative cached prompt tokens
                predict_total  INTEGER,              -- cumulative generated tokens
                prompt_tps     REAL,                 -- gauge, avg over scrape window
                predict_tps    REAL,
                busy           INTEGER               -- 1 if any slot is_processing at poll time
            )
            """
        )
        # migrate older DBs
        cols = {r[1] for r in conn.execute("PRAGMA table_info(metrics)")}
        if "busy" not in cols:
            conn.execute("ALTER TABLE metrics ADD COLUMN busy INTEGER")
        # keep ~6 months of samples
        cutoff = int((datetime.now(ZoneInfo("UTC")) - timedelta(days=180)).timestamp())
        conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))


# ------------------------------------------------------------- poller ------
def check_health() -> tuple[str, float | None]:
    """Return (status, latency_ms). status is 'up' or 'down'."""
    try:
        t0 = time.monotonic()
        r = requests.get(f"{LLAMA_SERVER_URL}/health", timeout=10)
        latency = (time.monotonic() - t0) * 1000
        if r.status_code == 200:
            return "up", latency
        return "down", latency
    except requests.RequestException:
        return "down", None


_METRIC_RE = re.compile(r"^llamacpp:(\w+) ([0-9.eE+-]+)$", re.M)


def slots_busy() -> int | None:
    """1 if any slot is_processing right now, 0 if all idle, None on error.
    Captured once per poll so 'thinking' minutes (busy but 0 token delta)
    can be told apart from truly idle minutes."""
    try:
        r = requests.get(f"{LLAMA_SERVER_URL}/slots", timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()
        sl = data.get("slots", []) if isinstance(data, dict) else data
        return 1 if any(s.get("is_processing") for s in sl) else 0
    except (requests.RequestException, ValueError):
        return None


def scrape_metrics() -> dict | None:
    """Fetch /metrics once. Returns parsed counters + process start time + busy flag,
    or None (endpoint disabled -> 501, server down, or parse failure)."""
    try:
        r = requests.get(f"{LLAMA_SERVER_URL}/metrics", timeout=10)
        if r.status_code != 200:
            return None
        vals = {k: float(v) for k, v in _METRIC_RE.findall(r.text)}
        return {
            "proc_start": int(r.headers.get("Process-Start-Time-Unix", 0)) or None,
            "prompt_total": vals.get("prompt_tokens_total"),
            "cached_total": vals.get("prompt_tokens_cached_total"),
            "predict_total": vals.get("tokens_predicted_total"),
            "prompt_tps": vals.get("prompt_tokens_seconds"),
            "predict_tps": vals.get("predicted_tokens_seconds"),
            "busy": slots_busy(),
        }
    except (requests.RequestException, ValueError):
        return None


def poller_loop() -> None:
    last_minute = -1
    while True:
        try:
            now = datetime.now(TZ).replace(second=0, microsecond=0)
            if int(now.timestamp()) != last_minute:
                status, latency = check_health()
                with db_conn() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO samples (ts, status, latency) VALUES (?, ?, ?)",
                        (int(now.timestamp()), status, latency),
                    )
                m = scrape_metrics() if status == "up" else None
                if m:
                    with db_conn() as conn:
                        conn.execute(
                            """INSERT OR REPLACE INTO metrics
                               (ts, proc_start, prompt_total, cached_total, predict_total, prompt_tps, predict_tps, busy)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (int(now.timestamp()), m["proc_start"], m["prompt_total"],
                             m["cached_total"], m["predict_total"], m["prompt_tps"], m["predict_tps"], m["busy"]),
                        )
                last_minute = int(now.timestamp())
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
    """Latest health sample + live server details (/props and /slots)."""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT ts, status, latency FROM samples ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    status, ts, latency = ("unknown", None, None) if row is None else (row["status"], row["ts"], row["latency"])

    details = None
    try:
        r = requests.get(f"{LLAMA_SERVER_URL}/props", timeout=5)
        if r.status_code == 200:
            p = r.json()
            details = {
                "model": p.get("model_alias") or p.get("model_path"),
                "ftype": p.get("model_ftype"),
                "n_ctx": (p.get("default_generation_settings") or {}).get("n_ctx"),
                "total_slots": p.get("total_slots"),
                "is_sleeping": p.get("is_sleeping"),
                "build_info": p.get("build_info"),
            }
    except requests.RequestException:
        pass

    slots = None
    try:
        r = requests.get(f"{LLAMA_SERVER_URL}/slots", timeout=5)
        if r.status_code == 200:
            data = r.json()
            slot_list = data.get("slots", []) if isinstance(data, dict) else data
            def slot_state(s):
                # newer/older builds may omit "state"; fall back to is_processing
                st = s.get("state")
                if st:
                    return st
                return "processing" if s.get("is_processing") else "idle"

            slots = [
                {"id": s.get("id"), "state": slot_state(s),
                 "n_ctx": s.get("n_ctx"), "model": s.get("model_alias")}
                for s in slot_list
            ]
    except requests.RequestException:
        pass

    return jsonify({
        "status": status,
        "ts": ts,
        "latency": latency,
        "server_url": LLAMA_SERVER_URL,
        "details": details,
        "slots": slots,
    })


def _minute_deltas(rows, col: str, lo: int, hi: int) -> dict[int, int]:
    """Per-minute deltas of a cumulative counter, restart-safe (see /api/day).
    rows: sqlite rows with ts, proc_start, <col>. Returns {ts: delta}."""
    by_ts = {r["ts"]: r for r in rows}
    out: dict[int, int] = {}
    for ts in range(lo, hi + 1, 60):
        cur = by_ts.get(ts)
        if cur is None or cur[col] is None:
            continue
        prev = by_ts.get(ts - 60)
        if prev is not None and prev[col] is not None:
            if prev["proc_start"] == cur["proc_start"]:
                d = int(cur[col] - prev[col])
                if d > 0:
                    out[ts] = d
            else:
                # server restarted between scrapes: counter ≈ tokens since restart
                out[ts] = int(cur[col])
        # else: first-ever row / collection gap -> unknown
    return out


@app.route("/api/calendar")
def api_calendar():
    """
    Per-hour status for a month (24 cells per day).
    Returns: { year, month, tz,
               cells:    { 'YYYY-MM-DD': ["up"/"down"/null, x24] },
               prompt_h: { 'YYYY-MM-DD': [int|null, x24] }  prefill tokens per hour,
               gen_h:    { 'YYYY-MM-DD': [int|null, x24] }  generated tokens per hour,
               day_totals, month_totals }
    A status cell is 'up' if >= 50% of its samples were up, 'down' if majority down,
    null if no samples in that hour.
    """
    year = request.args.get("year", type=int) or datetime.now(TZ).year
    month = request.args.get("month", type=int) or datetime.now(TZ).month
    if not 1 <= month <= 12:
        month = datetime.now(TZ).month

    first_day = datetime(year, month, 1, tzinfo=TZ)
    last_day = datetime(year, month, calendar.monthrange(year, month)[1], 23, 59, 59, tzinfo=TZ)

    rows = db_conn().execute(
        "SELECT ts, status FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (int(first_day.timestamp()), int(last_day.timestamp())),
    ).fetchall()
    db_conn().close()

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
            # a partial outage still shows the token shading of the recovered part
            ups = sum(1 for s in samples if s == "up")
            day_cells.append("up" if ups > 0 else "down")
        cells[key] = day_cells

    # per-30-min token sums (prefill / generated) + day & month totals
    mrows = db_conn().execute(
        """SELECT ts, proc_start, prompt_total, cached_total, predict_total
              FROM metrics WHERE ts >= ? AND ts <= ? ORDER BY ts""",
        (int(first_day.timestamp()) - 3600, int(last_day.timestamp())),
    ).fetchall()
    db_conn().close()

    p_dm = _minute_deltas(mrows, "prompt_total", int(first_day.timestamp()), int(last_day.timestamp()))
    g_dm = _minute_deltas(mrows, "predict_total", int(first_day.timestamp()), int(last_day.timestamp()))

    def by_day_h(dm):
        grouped: dict[str, list[tuple[int, int]]] = {}
        for ts, v in dm.items():
            dtm = datetime.fromtimestamp(ts, TZ)
            grouped.setdefault(dtm.strftime("%Y-%m-%d"), []).append((dtm.hour, v))
        return grouped

    p_grp, g_grp = by_day_h(p_dm), by_day_h(g_dm)
    prompt_h: dict[str, list[int | None]] = {}
    gen_h: dict[str, list[int | None]] = {}
    day_totals: dict[str, dict[str, int]] = {}
    month_prompt = month_gen = 0
    for day in range(calendar.monthrange(year, month)[1]):
        key = (first_day + timedelta(days=day)).strftime("%Y-%m-%d")
        ps, gs = [None] * 24, [None] * 24
        for s30, v in p_grp.get(key, []):
            ps[s30] = (ps[s30] or 0) + v
        for s30, v in g_grp.get(key, []):
            gs[s30] = (gs[s30] or 0) + v
        dp, dg = sum(v or 0 for v in ps), sum(v or 0 for v in gs)
        prompt_h[key], gen_h[key], day_totals[key] = ps, gs, {"prompt": dp, "gen": dg}
        month_prompt, month_gen = month_prompt + dp, month_gen + dg

    return jsonify({
        "year": year, "month": month, "tz": TIMEZONE, "cells": cells,
        "prompt_h": prompt_h, "gen_h": gen_h,
        "day_totals": day_totals,
        "month_totals": {"prompt": month_prompt, "gen": month_gen},
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

    start, end = int(day.timestamp()), int(day.timestamp()) + 86399
    rows = db_conn().execute(
        "SELECT ts, status FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (start, end),
    ).fetchall()
    db_conn().close()

    by_min = {}
    for r in rows:
        dt = datetime.fromtimestamp(r["ts"], TZ)
        by_min[dt.hour * 60 + dt.minute] = r["status"]

    cells = [by_min.get(i) for i in range(1440)]

    # per-minute token deltas from cumulative /metrics counters.
    # Restart-safe: only diff consecutive rows with the same proc_start;
    # on a proc_start change record the counter value itself (tokens since boot).
    day_start, day_end = int(day.timestamp()), int(day.timestamp()) + 86399
    rows = db_conn().execute(
        """SELECT ts, proc_start, prompt_total, cached_total, predict_total, busy
              FROM metrics WHERE ts > ? AND ts <= ? ORDER BY ts""",
        (day_start - 3600, day_end),
    ).fetchall()
    db_conn().close()

    def deltas(col):
        dm = _minute_deltas(rows, col, day_start, day_end)
        return [dm.get(day_start + i * 60) for i in range(1440)]

    prompt_min = deltas("prompt_total")
    cached_min = deltas("cached_total")
    gen_min = deltas("predict_total")
    busy_by_ts = {r["ts"]: r["busy"] for r in rows}
    busy_min = [busy_by_ts.get(day_start + i * 60) for i in range(1440)]
    return jsonify({
        "date": date_str, "tz": TIMEZONE, "cells": cells,
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
    init_db()
    threading.Thread(target=poller_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "5000"))
    print(f"Llama heatmap: http://localhost:{port}  (watching {LLAMA_SERVER_URL}, every {POLL_INTERVAL}s)")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
