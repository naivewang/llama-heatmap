"""GET /api/calendar — per-hour status + token sums for a month (24 cells/day).

A cell is 'sleep' when EVERY sample in the hour was asleep, 'up' when any
sample was up (token shading then applies), 'down' when the hour was totally
dead (no up, no sleep), null if no samples at all.
"""
import calendar
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from core.deltas import minute_deltas


def register(bp: Blueprint, ctx) -> None:
    @bp.route("/calendar")
    def month():
        year = request.args.get("year", type=int) or datetime.now(ctx.tz).year
        month = request.args.get("month", type=int) or datetime.now(ctx.tz).month
        if not 1 <= month <= 12:
            month = datetime.now(ctx.tz).month

        first_day = datetime(year, month, 1, tzinfo=ctx.tz)
        last_day = datetime(year, month, calendar.monthrange(year, month)[1], 23, 59, 59, tzinfo=ctx.tz)

        rows = ctx.store.samples_range(int(first_day.timestamp()), int(last_day.timestamp()))

        # bucket by (date_str, hour)
        buckets: dict[tuple[str, int], list[str]] = {}
        for r in rows:
            dt = datetime.fromtimestamp(r["ts"], ctx.tz)
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
        mrows = ctx.store.metrics_range(int(first_day.timestamp()) - 3600, int(last_day.timestamp()))

        p_dm = minute_deltas(mrows, "prompt_total", int(first_day.timestamp()), int(last_day.timestamp()))
        g_dm = minute_deltas(mrows, "predict_total", int(first_day.timestamp()), int(last_day.timestamp()))
        c_dm = minute_deltas(mrows, "cached_total", int(first_day.timestamp()), int(last_day.timestamp()))

        # TRUE statistics: every real minute counts. On the DST fall-back day the
        # repeated 1 AM hour contributes TWICE to hour/day/month totals (real work
        # is real work). Only the 60 display cells can't show both occurrences.
        def by_day_h(dm):
            grouped: dict[str, list[tuple[int, int]]] = {}
            for ts, v in dm.items():
                dtm = datetime.fromtimestamp(ts, ctx.tz)
                grouped.setdefault(dtm.strftime("%Y-%m-%d"), []).append((dtm.hour * 60 + dtm.minute, v))
            return grouped

        def by_day_total(dm):
            out: dict[str, int] = {}
            for ts, v in dm.items():
                d = datetime.fromtimestamp(ts, ctx.tz).strftime("%Y-%m-%d")
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
            "year": year, "month": month, "tz": ctx.tz_name, "cells": cells,
            "prompt_h": prompt_h, "gen_h": gen_h,
            "day_totals": day_totals,
            "month_totals": {"prompt": month_prompt, "gen": month_gen, "cached": month_cached},
        })
