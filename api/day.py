"""GET /api/day — per-minute status + token deltas for one day.

?date=YYYY-MM-DD (default: today)
"""
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from core.deltas import minute_deltas


def register(bp: Blueprint, ctx) -> None:
    @bp.route("/day")
    def day():
        date_str = request.args.get("date") or datetime.now(ctx.tz).strftime("%Y-%m-%d")
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=ctx.tz)
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

        rows = ctx.store.samples_range(day_start, day_end, hi_inclusive=False)

        by_off = {}
        for r in rows:
            by_off[(r["ts"] - day_start) // 60] = r["status"]
        cells = [by_off.get(i) for i in range(n_min)]

        # per-minute token deltas from cumulative /metrics counters.
        # Restart-safe: only diff consecutive rows with the same proc_start;
        # on a proc_start change record the counter value itself (tokens since boot).
        rows = ctx.store.metrics_range(day_start - 3600, day_end, hi_inclusive=False)

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
            dt = datetime.fromtimestamp(day_start + i * 3600, ctx.tz)
            hour_labels.append(f"{dt.hour:02d}" + ("\u2032" if dt.hour == prev_h else ""))
            prev_h = dt.hour

        return jsonify({
            "date": date_str, "tz": ctx.tz_name,
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
