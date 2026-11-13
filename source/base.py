"""Engine-source interface.

An EngineSource is anything that can report on one AI server: its health,
its cumulative token counters, and display-only details. Everything else
(store, core, API, UI) only ever sees the normalized Snapshot below — that
is the entire contract a new engine (vLLM, Ollama, an on-prem gateway…)
has to fill. One new file in this package, zero edits elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class Snapshot:
    """One metrics scrape, normalized to engine-agnostic counters."""
    proc_start: Optional[int]    # process start time (restart detector); None if unknown
    prefill: Optional[int]       # cumulative prompt tokens processed (excl. cache)
    generated: Optional[int]     # cumulative generated tokens
    cached: Optional[int]        # cumulative prompt tokens served from KV cache
    prefill_tps: Optional[float] # gauge, average over the scrape window
    generated_tps: Optional[float]
    busy: int                    # 1 if inference is in flight at scrape time


class EngineSource(Protocol):
    def health(self) -> tuple[str, Optional[float]]:
        """(status, latency_ms); status in 'up' | 'sleep' | 'down'."""
        ...

    def metrics(self) -> Optional[Snapshot]:
        """Latest counters, or None if the endpoint is unavailable."""
        ...

    def details(self) -> Optional[dict]:
        """Display-only server info (model, ctx, slots, build…); None if down."""
        ...
