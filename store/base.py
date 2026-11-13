"""Store interface.

Minute-grained persistence for health samples and metric snapshots.
Implementations must return PLAIN DICTS (keys: ts, status, latency for
samples; ts, proc_start, prompt_total, cached_total, predict_total,
prompt_tps, predict_tps, busy for metrics) so the core logic never touches
a DB driver.

A new backend (Postgres, DuckDB, InfluxDB…) = one new file in this package
implementing these methods; nothing else in the app changes.
"""
from __future__ import annotations

from typing import Optional, Protocol


class Store(Protocol):
    def init(self) -> None:
        """Create schema / migrate / prune. Called once at startup."""
        ...

    def add_sample(self, ts: int, status: str, latency: Optional[float]) -> None: ...

    def add_metrics(self, ts: int, snap) -> None:
        """Persist one source.Snapshot (duck-typed to avoid a circular import)."""
        ...

    def samples_range(self, lo: int, hi: int, hi_inclusive: bool = True) -> list[dict]: ...

    def metrics_range(self, lo: int, hi: int, hi_inclusive: bool = True) -> list[dict]: ...

    def latest_sample(self) -> Optional[dict]: ...

    def latest_busy(self) -> Optional[int]: ...
