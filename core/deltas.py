"""Pure per-minute delta logic — no DB, no engine, no I/O.

Operates on plain row dicts with ts, proc_start and the counter column.
"""


def minute_deltas(rows: list[dict], col: str, lo: int, hi: int) -> dict[int, int]:
    """Per-minute deltas of a cumulative counter, restart-safe (see /api/day).

    rows: plain dicts with ts, proc_start, <col>. Returns {ts: delta}.
    """
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
