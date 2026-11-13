"""Shared context for the API handlers.

The handlers are defined in sibling modules and wired onto a Blueprint by
create_api(); they reach the engine, the store and the config only through
this context, so the package imports nothing from app.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from source.base import EngineSource
from store.base import Store


@dataclass
class ApiContext:
    source: EngineSource
    store: Store
    server_url: str      # shown in the UI / status payload
    tz_name: str         # e.g. "America/Los_Angeles" — echoed to the client
    tz: ZoneInfo         # all time bucketing happens in this zone
