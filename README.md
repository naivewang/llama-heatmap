# llama-heatmap

A tiny self-hosted dashboard for a [llama.cpp](https://github.com/ggml-org/llama.cpp)
server (`llama-server`): it polls the server every 60 seconds, stores everything
in SQLite, and renders a monthly calendar with **GitHub-contribution-style
bubbles** showing uptime and token throughput — prefill (blue) vs. generated
(green) — with drill-down to per-minute detail.

No external services needed: one Python file, one template, one SQLite file.

<img width="1287" height="1224" alt="image" src="https://github.com/user-attachments/assets/d7feb0de-406b-4ace-a939-cedf5e621017" />


## Features

- **Health monitoring** — polls `GET /health` every 60 s; per-hour up/down/no-data
  status per day, plus live status card (model, build, slot state, latency).
- **Token throughput** — scrapes the llama.cpp `/metrics` endpoint
  (requires `--metrics` on the server) and computes per-minute deltas of
  `prompt_tokens_processed_total` / `prompt_tokens_cached_total` /
  `tokens_predicted_total`.
  - **Restart-safe**: the `Process-Start-Time-Unix` response header is tracked;
    when the counter resets (server restart), the delta is derived from the new
    counter value instead of going negative. Gaps/restarts never corrupt history.
- **Calendar view** — one bubble per hour (4 rows × 6 cols), each bubble is a
  golden-ratio rectangle split into two golden sub-rectangles:
  - **left (blue shades)** = prefill tokens in that hour
  - **right (green shades)** = generated tokens in that hour
  - 5 GitHub-style shade levels, √-scaled to the month max
  - 🔴 red = server down that hour · light grey = up, 0 tokens · dark grey = no data
- **Detail views** — click an hour bubble → its 60 per-minute cells;
  click a day number → the full day (1440 cells, scrollable). Both show
  hour/day totals (prefill & generated).
- **Totals everywhere** — per hour (tooltip), per day (tooltip + detail header),
  per month (calendar header).
- **Auto-refresh** every 60 s, timezone-correct "now" highlighting
  (server timezone, default `America/Los_Angeles`).
- ~6 months of history retained automatically.

## Quickstart

```bash
git clone https://github.com/naivewang/llama-heatmap.git
cd llama-heatmap
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export LLAMA_SERVER_URL=http://YOUR_LLAMA_HOST:8080   # your llama-server
.venv/bin/python app.py
# → open http://localhost:5000  (binds 0.0.0.0, reachable on your LAN)
```

The llama.cpp server side:

```bash
llama-server -m model.gguf --metrics   # --metrics enables /metrics
```

`/metrics` is optional — if it's absent (501), the heatmap still tracks
up/down status; token bubbles simply stay grey.

## Configuration

| Env var            | Default                  | Meaning                              |
|--------------------|--------------------------|--------------------------------------|
| `LLAMA_SERVER_URL` | `http://127.0.0.1:8080`  | Base URL of the llama.cpp server     |
| `MONITOR_TZ`       | `America/Los_Angeles`    | IANA timezone for calendar/time math |

Heatmap HTTP port: `app.run(port=5000)` at the bottom of `app.py`.

## API

| Endpoint                  | Returns                                                              |
|---------------------------|----------------------------------------------------------------------|
| `/api/status`             | Latest health sample + live `/props` & `/slots` (model, slots, build) |
| `/api/calendar?year&month`| Per-hour status + per-hour prefill/generated tokens, day & month totals |
| `/api/day?date=YYYY-MM-DD`| Per-minute status + per-minute token deltas + day totals             |

## How it works

- A daemon thread appends one row per poll to SQLite:
  - `samples(ts, status, latency)` — from `/health`
  - `metrics(ts, proc_start, prompt_total, cached_total, predict_total, ...)`
    — cumulative counters from `/metrics`, stored as-is.
- **Deltas are computed at read time** from consecutive rows: normal minute →
  `cur - prev`; `proc_start` changed → `cur` (≈ tokens since restart);
  first row / gap → `null`. This makes the history self-healing: lost polls or
  restarts never produce garbage, they just show "no data" for that minute.
- Rows older than ~6 months are pruned at startup.

## Run as a service (optional)

`~/.config/systemd/user/llama-heatmap.service`:

```ini
[Unit]
Description=llama.cpp server monitor
After=network-online.target

[Service]
WorkingDirectory=%h/llama-heatmap
Environment=LLAMA_SERVER_URL=http://YOUR_LLAMA_HOST:8080
ExecStart=%h/llama-heatmap/.venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now llama-heatmap
loginctl enable-linger $USER    # start at boot even without a login
```

## Project layout

```
app.py            # Flask app: polling thread, SQLite, API endpoints
templates/
  index.html      # Single-page dashboard (vanilla JS, no build step)
requirements.txt
monitor.db        # created at runtime (gitignored)
```

## Notes & limitations

- Uses the Flask dev server — fine for a single-user LAN dashboard; put
  gunicorn behind it if you need more.
- Token data only exists from when `/metrics` became reachable; earlier
  calendar cells show status only.
- `Process-Start-Time-Unix` is treated purely as a *change detector*
  (its value is origin-relative microseconds, not real Unix time).
