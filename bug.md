# BUG: background poller calls `GET /slots` every 60s — prevents llama.cpp server from sleeping

> TEMPORARY FILE — delete this file once the fix below has been applied.

## Symptom

The watched llama.cpp server (`LLAMA_SERVER_URL`, e.g. `http://10.0.0.78:8080`) runs with
`--sleep-idle-seconds 300` but **never enters sleep state**, even after hours with zero
inference requests. `GET /props` reports `"is_sleeping": false` indefinitely.

## Root cause

llama.cpp's `GET /slots` is **not a passive read**: it posts a `SERVER_TASK_TYPE_SLOT_GET`
to the server's task queue and waits for the worker to answer
(`llama.cpp: tools/server/server-context.cpp`, `get_slots`).

1. Only `METRICS` tasks are exempt from the idle timer
   (`llama.cpp: tools/server/server-queue.cpp`, `task_resets_idle_timer()`).
   So **every `/slots` poll resets the `--sleep-idle-seconds` idle timer** — with a
   60s poll interval and a 300s sleep timeout, the server can never reach idle.
2. `get_slots` uses `create_response()` without `bypass_sleep`, so a `/slots` poll on a
   **sleeping** server also wakes it and forces a full model reload (weights + KV rebuild).

This heatmap's background loop (`poller_loop` → `scrape_metrics` → `slots_busy()`) calls
`GET /slots` once per minute, forever — even when nobody has the page open. That is what
keeps the server awake.

The exempt (sleep-safe, timer-safe) endpoints are exactly: `/health`, `/props`,
`/models`, `/metrics`.

## Fix (only `app.py`; no DB migration)

### 1. Delete `slots_busy()` entirely

It is only called from `scrape_metrics()`. (`/api/status` has its own inline `/slots`
call — leave that one: it is on-demand, triggered by a human viewing the page, and waking
the server on a human view is acceptable.)

### 2. Rewrite `scrape_metrics()` — derive `busy` from the `/metrics` window throughput

```python
def scrape_metrics() -> dict | None:
    """Fetch /metrics once. Returns parsed counters + process start time + busy flag,
    or None (endpoint disabled -> 501, server down, or parse failure).

    busy is derived from the scrape-window throughput gauges (>0 = the server
    did real prefill/decode work since the last scrape). NOTE: do NOT use
    /slots here — it posts a task to the llama.cpp server, which resets the
    --sleep-idle-seconds idle timer and wakes a sleeping server. /slots may
    only be called on-demand (see /api/status)."""
    try:
        r = requests.get(f"{LLAMA_SERVER_URL}/metrics", timeout=10)
        if r.status_code != 200:
            return None
        vals = {k: float(v) for k, v in _METRIC_RE.findall(r.text)}
        prompt_tps  = vals.get("prompt_tokens_seconds", 0.0)
        predict_tps = vals.get("predicted_tokens_seconds", 0.0)
        return {
            "proc_start": int(r.headers.get("Process-Start-Time-Unix", 0)) or None,
            "prompt_total": vals.get("prompt_tokens_total"),
            "cached_total": vals.get("prompt_tokens_cached_total"),
            "predict_total": vals.get("tokens_predicted_total"),
            "prompt_tps": prompt_tps,
            "predict_tps": predict_tps,
            "busy": 1 if (prompt_tps > 0 or predict_tps > 0) else 0,
        }
    except (requests.RequestException, ValueError):
        return None
```

### 3. Update the `metrics.busy` schema comment (cosmetic)

```sql
busy  INTEGER   -- 1 if /metrics window showed prefill/decode activity
```

(Old comment said "1 if any slot is_processing at poll time".)

## Deliberately NOT changed

- `/api/status` keeps its live `/slots` call (on-demand, human-triggered only).
- DB schema, `poller_loop()`, all API routes: untouched.
- No new columns, no migrations.

## Known semantic shifts (accepted)

- `busy` now means "compute happened in the last ~60s scrape window" instead of
  "a slot was `is_processing` at the poll instant". A request that starts and finishes
  within one window, or a slot blocked with no tokens moving, reads as not-busy.
  Fine for heatmap purposes.
- The first `/metrics` scrape after the server enters sleep replays the pre-sleep
  throughput bucket (llama.cpp server-side behavior), so at most one "sleep" minute
  may show `busy=1`. Ignorable.

## Verification (after deploying, server must have `--sleep-idle-seconds N`)

1. Leave the heatmap running, no other clients, no browser tabs on this page,
   wait > N + 2 poll intervals.
2. `curl $LLAMA_SERVER_URL/props | grep is_sleeping` → must now report `true`.
   (This is the actual regression test — it was stuck `false` before the fix.)
3. Heatmap page/calendar still renders; `/api/day` `busy_min` shows 1s only on
   minutes with token activity.
4. llama.cpp server log shows `### sleeping: entering sleeping state`, and no
   `exiting sleeping state` until a real request arrives or a human opens the
   heatmap page.
