"""GET /api/status — latest health sample + live server details.

Hit by the page's 59s auto-refresh, so it must stay sleep-safe: slot state
is SYNTHESIZED from the last poller scrape's busy flag — never call /slots
here (it posts a task to llama.cpp, which resets --sleep-idle-seconds and
would wake a sleeping server). A <=60s-stale state is fine for a monitor.
"""
from flask import Blueprint, jsonify


def register(bp: Blueprint, ctx) -> None:
    @bp.route("/status")
    def status():
        row = ctx.store.latest_sample()
        status, ts, latency = (
            ("unknown", None, None) if row is None
            else (row["status"], row["ts"], row["latency"])
        )

        details = ctx.source.details()

        slots = None
        if details and details.get("total_slots"):
            if details.get("is_sleeping"):
                state = "asleep"
            else:
                busy = ctx.store.latest_busy()
                state = "processing" if busy else "idle"
            slots = [{"id": i, "state": state} for i in range(details["total_slots"])]

        return jsonify({
            "status": status,
            "ts": ts,
            "latency": latency,
            "server_url": ctx.server_url,
            "tz": ctx.tz_name,
            "details": details,
            "slots": slots,
        })
