"""llama.cpp as an EngineSource.

Only sleep-safe endpoints are touched: /health, /props, /metrics.
/slots is NEVER called from the monitor — it posts a task to llama.cpp's
queue, which resets --sleep-idle-seconds and would wake a sleeping server.
"""
import re
import time

import requests

from .base import Snapshot

_METRIC_RE = re.compile(r"^llamacpp:(\w+) ([0-9.eE+-]+)$", re.M)


class LlamaCppSource:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def _asleep(self) -> bool | None:
        """True/False from /props, None if unreachable. /health answers 200 even
        while the server is asleep, so sleep can only be told from this flag."""
        try:
            r = requests.get(f"{self.url}/props", timeout=5)
            if r.status_code == 200:
                return bool(r.json().get("is_sleeping"))
        except (requests.RequestException, ValueError):
            pass
        return None

    def health(self) -> tuple[str, float | None]:
        """Return (status, latency_ms). status is 'up', 'sleep' or 'down'."""
        try:
            t0 = time.monotonic()
            r = requests.get(f"{self.url}/health", timeout=10)
            latency = (time.monotonic() - t0) * 1000
            if r.status_code != 200:
                return "down", latency
            return ("sleep" if self._asleep() else "up"), latency
        except requests.RequestException:
            return "down", None

    def metrics(self) -> Snapshot | None:
        """Fetch /metrics once. Returns normalized counters + process start time
        + busy flag, or None (endpoint disabled -> 501, server down, parse
        failure).

        busy comes from the requests_processing gauge (in-flight requests) — the
        true 'inferencing in flight' signal, and sleep-safe: /metrics is one of
        the timer-exempt endpoints, unlike /slots (which would reset
        --sleep-idle-seconds and wake a sleeping server). A request waiting
        through a wake/reload shows in requests_deferred, NOT processing, so
        recovery minutes correctly stay non-purple.
        Old builds without the gauge fall back to window throughput > 0."""
        try:
            r = requests.get(f"{self.url}/metrics", timeout=10)
            if r.status_code != 200:
                return None
            vals = {k: float(v) for k, v in _METRIC_RE.findall(r.text)}
            prefill_tps = vals.get("prompt_tokens_seconds", 0.0)
            generated_tps = vals.get("predicted_tokens_seconds", 0.0)
            rp = vals.get("requests_processing")
            busy = (1 if rp > 0 else 0) if rp is not None \
                   else (1 if (prefill_tps > 0 or generated_tps > 0) else 0)

            def _int(k):
                v = vals.get(k)
                return int(v) if v is not None else None

            return Snapshot(
                proc_start=int(r.headers.get("Process-Start-Time-Unix", 0)) or None,
                prefill=_int("prompt_tokens_total"),
                cached=_int("prompt_tokens_cached_total"),
                generated=_int("tokens_predicted_total"),
                prefill_tps=prefill_tps,
                generated_tps=generated_tps,
                busy=busy,
            )
        except (requests.RequestException, ValueError):
            return None

    def details(self) -> dict | None:
        """Display-only server info from /props (model, ftype, ctx, slots, build)."""
        try:
            r = requests.get(f"{self.url}/props", timeout=5)
            if r.status_code == 200:
                p = r.json()
                return {
                    "model": p.get("model_alias") or p.get("model_path"),
                    "ftype": p.get("model_ftype"),
                    "n_ctx": (p.get("default_generation_settings") or {}).get("n_ctx"),
                    "total_slots": p.get("total_slots"),
                    "is_sleeping": p.get("is_sleeping"),
                    "build_info": p.get("build_info"),
                }
        except requests.RequestException:
            pass
        return None
