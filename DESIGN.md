# llama-heatmap — System Design

A one-process dashboard that turns a llama.cpp server into a
GitHub-contribution-style calendar: **one bubble per hour**, split into
**prefill (blue)** and **generated (green)**, with drill-down to the minute.
One Python file, one template, one SQLite file — no external services.

This document describes the system as built, and *why* it is shaped the way
it is. The design is driven by a single hard constraint; everything else
follows from it.

---

## 1. First principle: the monitor must be invisible to the server

The monitored server runs with `--sleep-idle-seconds 300`: after 5 idle
minutes it offloads the model to RAM to save power. llama.cpp's task queue
resets that idle timer for **every task that is not timer-exempt** — and a
`GET /slots` is such a task. Polling `/slots` would (a) keep the server
perpetually awake and (b) wake it on every poll. That is the bug this
project was born from, and the rule it enforces:

> **The monitor only ever calls the timer-exempt endpoints:
> `/health`, `/props`, `/models`, `/metrics`.
> `/slots` is never called — not by the poller, not by the auto-refresh path.**

Every architectural choice below exists because of this rule.

## 2. Architecture

```
                 every 60 s                       read time
┌────────────┐  /health /props /metrics   ┌──────────────┐   ┌───────────┐
│ llama.cpp  │ ─────────────────────────► │  SQLite      │ ◄─┤  API      │
│  server    │   (timer-exempt only)      │  samples     │   │  /api/*   │
└────────────┘                            │  metrics     │   └─────┬─────┘
                                          └──────────────┘         │
                                              counters,            │ JSON
                                              stored as-is         ▼
                                                        ┌────────────────────┐
                                                        │  Browser (SPA)     │
                                                        │  vanilla JS,       │
                                                        │  refreshes every   │
                                                        │  59 s              │
                                                        └────────────────────┘
```

- **Poller** — daemon thread, one scrape per minute (aligned to the minute
  boundary). Writes one row per table; sleeps 60 s between polls.
- **Storage** — two tables, both keyed by minute (`ts` truncated to 60 s):

  | table     | contents                                                        |
  |-----------|-----------------------------------------------------------------|
  | `samples` | `ts, status ('up'\|'sleep'\|'down'), latency` — from `/health` + `/props` |
  | `metrics` | `ts, proc_start, prompt_total, cached_total, predict_total, prompt_tps, predict_tps, busy` — raw counters from `/metrics` |

  Raw **counters** are stored, never deltas: history stays immutable and
  deltas are recomputed at read time (§4). Rows older than ~6 months are
  pruned at startup.
- **API** — `/api/status` (live card), `/api/calendar?year&month`
  (24 h-cells per day + day/month totals), `/api/day?date=…` (1440
  minute-cells + totals). Each request is a pure read over the two tables
  plus, for `/api/status`, one live `/props` peek.
- **Frontend** — single template, no build step. All time-dependent
  rendering (which cell is "now", hour labels, tooltips) is computed in the
  **server's timezone** (`MONITOR_TZ`, default `America/Los_Angeles`) from
  the `tz` field in the API — so every viewer, wherever they sit, sees the
  same board.

## 3. Signals: one per question, all sleep-safe

| Question the board must answer        | Signal                                                        | Source        |
|---------------------------------------|---------------------------------------------------------------|---------------|
| Is the server reachable?              | HTTP 200 on `/health` (+ latency)                             | `/health`     |
| Is it asleep? (health answers 200 even asleep) | `is_sleeping`                                           | `/props`      |
| How many prompt / generated tokens?   | deltas of `prompt_tokens_total`, `tokens_predicted_total`     | `/metrics`    |
| How many were KV-cache hits?          | delta of `prompt_tokens_cached_total`                         | `/metrics`    |
| Is inference **in flight** right now? | `requests_processing` gauge > 0                               | `/metrics`    |
| Did the server restart?               | `Process-Start-Time-Unix` header changed                      | `/metrics`    |

Two subtleties:

- **`busy` is a gauge, sampled at scrape time** — the instantaneous
  "requests in flight" count. That is exactly the semantic the UI wants
  ("inferencing happening *in flight*"), and it comes from the same
  sleep-safe response as the counters: **no second request, no `/slots`**.
- **A request waiting through a wake/reload is *deferred*, not
  processing** (`requests_deferred` gauge). So recovery minutes are not
  marked busy — the board never claims inference for model reload. Old
  builds without the gauge fall back to "window throughput > 0".

The slot badge in the status card is *synthesized* from the last `busy`
flag (≤ 60 s stale) — never a live slot query.

## 4. Delta computation: restart-safe by construction

Counters are stored as-is; a minute's token delta is computed at read time
by comparing consecutive `metrics` rows:

```
same proc_start as previous row   →  cur − prev        (normal minute)
proc_start changed (server restarted) → cur            (≈ tokens since restart)
no previous row / time gap        →  null              ("no data")
```

Consequences:

- **Restart-safe**: `proc_start` is used purely as a *change detector*
  (its value is origin-relative, never for time math). A restart can never
  produce a negative delta.
- **Self-healing**: missed polls or restarts render as "no data" for that
  minute — never garbage in the history.
- **Immutable history**: cells never mutate after their minute closes
  (except the live, in-progress minute, by design).

## 5. Time: one clock, deliberately off-phase

- **Bucketing** — everything is bucketed by the minute in `MONITOR_TZ`;
  the backend owns all time math, the browser only renders.
- **Poll cadence** — 60 s, aligned to the minute boundary.
- **Refresh cadence** — the page refetches every **59 s**. Deliberately
  *not* 60: two periodic processes with the same period would lock into
  resonance (the page always catching the same stale instant). One second
  of phase drift per minute keeps the view fresh against the poll.
- **Cell stability** — an hour bubble shows the delta of the *latest*
  scrape in that hour, so a cell's value can tick while its hour is still
  live and then freezes. This "latest scrape wins" rule was chosen over
  stricter window attribution because for a live dashboard, **a cell must
  never visibly shift under the user's eye**.

## 6. Visual language: state first, volume second

Each bubble answers two questions in order: *what state was the server in?*
then *how much flowed?*

| State (hour / minute)                        | Rendering                                    |
|----------------------------------------------|----------------------------------------------|
| **down** — zero `up` samples in the window   | solid **red**                                |
| **sleep** — all samples `asleep`             | solid **warm (yellow-tinted) grey** — muted, close to idle grey but warmer |

| **up, in flight, 0 tokens across the meter** | solid **purple** (`busy=1`, no delta) — a long prefill mid-flight is the canonical case |
| **up, idle, 0 tokens**                       | solid **light grey**                         |
| **up, tokens moved**                         | split bubble: **blue** half = prefill, **green** half = generated, each shaded in 5 GitHub-style levels, √-scaled to the month max (keeps small hours visible) |
| **no data**                                  | **dark grey**                                |

Design notes:

- **Golden-ratio geometry.** Each half of a calendar bubble is a
  golden portrait (w : h = 1 : 1.618); the combined bubble is a 1.236 : 1
  rectangle with a flat inner seam and rounded outer corners. The detail
  view uses the *same* outer ratio, split top/bottom (top = prefill,
  bottom = generated) — same shape, rotated split. All sizing is fluid
  (CSS grid + `aspect-ratio`, container-query fonts); there are no fixed
  pixel sizes.
- **Red means "totally dead"** — an hour is down only with *zero*
  reachable samples; a single up-sample keeps it alive.
- **Cache % is a day/month fact, not an hour fact** — ~99% of prompt
  tokens on this deployment are KV-cache hits, so at hour level the blue
  half is (correctly) almost always dim; the hit-rate is shown where it is
  meaningful: day and month headers.
- **Themes.** Dark and light palettes (toggle, persisted in
  `localStorage`); the shade ramp is defined per theme so both read
  GitHub-style. Short legend, arrow tooltips (↑ prefill / ↓ generated),
  plain `HH` hour labels that are zero-height (they never stretch rows)
  and sparse on phones.

## 7. Decisions & trade-offs (the short version)

| Decision | Why |
|----------|-----|
| **Never call `/slots`** | It resets `--sleep-idle-seconds` and wakes sleeping servers. All state is derivable from the exempt endpoints; the slot badge is synthesized. |
| **`busy` from `requests_processing`** | It *is* the in-flight count, delivered inside the already-sleep-safe `/metrics` response — no extra call, no timer impact, and it keeps "recovery" (deferred) distinct from "inferencing". |
| **Counters at rest, deltas at read time** | Immutable, restart-safe, self-healing history (§4). |
| **59 s refresh vs 60 s poll** | Avoids resonance; keeps the live minute honest. |
| **Latest-scrape-wins cells** | Cells must not shift under the viewer's eye — stability beats semantic perfection for a live board. |
| **Server-timezone rendering** | One canonical "now" for every viewer, LAN or remote. |
| **SQLite + Flask dev server + nohup** | Single-user LAN tool: zero ops. No systemd, no migrations framework, no build step. |

### Known, accepted residuals

- `requests_processing` is instantaneous: a request shorter than one
  scrape interval is missed by the *gauge* — but its tokens still paint
  blue/green, so the board never under-reports work, only the purple.
- The first `/metrics` scrape after a wake may replay the pre-sleep
  throughput bucket (cosmetic; affects tps display only, not colors).
- Token bubbles exist only from when `/metrics` was first reachable;
  earlier history shows status only.
