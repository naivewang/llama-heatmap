"""API package: the JSON contract the frontend depends on.

create_api() builds the /api Blueprint from one module per endpoint:

    status.py   GET /api/status     latest health + live details + slot badge
    month.py    GET /api/calendar   per-hour status + token sums for a month
    day.py      GET /api/day        per-minute status + token deltas for a day

Handlers reach the engine/store/config only through the ApiContext
(base.py) — this package imports nothing from app.py.
"""
from flask import Blueprint

from .base import ApiContext
from . import day, month, status

__all__ = ["ApiContext", "create_api"]


def create_api(ctx: ApiContext) -> Blueprint:
    bp = Blueprint("api", __name__, url_prefix="/api")
    status.register(bp, ctx)
    month.register(bp, ctx)
    day.register(bp, ctx)
    return bp
