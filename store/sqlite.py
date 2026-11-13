"""SQLite store: the default (and, for a personal monitor, the right)
persistence. Two tables, minute-grained:

    samples(ts, status, latency)
    metrics(ts, proc_start, prompt_total, cached_total, predict_total,
            prompt_tps, predict_tps, busy)

Rows come back as plain dicts so core logic never sees a sqlite3.Row.
Retention (default ~6 months) is pruned at startup.
"""
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class SqliteStore:
    def __init__(self, path: str, retention_days: int = 180):
        self.path = path
        self.retention_days = retention_days

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    ts        INTEGER NOT NULL,   -- unix epoch seconds, truncated to minute
                    status    TEXT    NOT NULL,   -- 'up' | 'sleep' | 'down'
                    latency   REAL,               -- ms, NULL when down
                    PRIMARY KEY (ts)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples (ts)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metrics (
                    ts             INTEGER PRIMARY KEY,  -- unix epoch seconds, truncated to minute
                    proc_start     INTEGER,              -- Process-Start-Time-Unix (restart detector)
                    prompt_total   INTEGER,              -- cumulative prompt tokens (excl. cache)
                    cached_total   INTEGER,              -- cumulative cached prompt tokens
                    predict_total  INTEGER,              -- cumulative generated tokens
                    prompt_tps     REAL,                 -- gauge, avg over scrape window
                    predict_tps    REAL,
                    busy           INTEGER               -- 1 if requests_processing > 0 at scrape time (in-flight inference)
                )
                """
            )
            # migrate older DBs
            cols = {r[1] for r in conn.execute("PRAGMA table_info(metrics)")}
            if "busy" not in cols:
                conn.execute("ALTER TABLE metrics ADD COLUMN busy INTEGER")
            # keep ~6 months of samples
            cutoff = int((datetime.now(ZoneInfo("UTC"))
                          - timedelta(days=self.retention_days)).timestamp())
            conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))

    def add_sample(self, ts: int, status: str, latency: float | None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO samples (ts, status, latency) VALUES (?, ?, ?)",
                (ts, status, latency),
            )

    def add_metrics(self, ts: int, s) -> None:
        """s: a source.Snapshot (duck-typed to avoid a circular import)."""
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO metrics
                   (ts, proc_start, prompt_total, cached_total, predict_total, prompt_tps, predict_tps, busy)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, s.proc_start, s.prefill, s.cached, s.generated,
                 s.prefill_tps, s.generated_tps, s.busy),
            )

    def _range(self, table: str, cols: str, lo: int, hi: int,
               hi_inclusive: bool) -> list[dict]:
        op = "<=" if hi_inclusive else "<"
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {cols} FROM {table} WHERE ts >= ? AND ts {op} ? ORDER BY ts",
                (lo, hi),
            ).fetchall()
        return [dict(r) for r in rows]

    def samples_range(self, lo: int, hi: int, hi_inclusive: bool = True) -> list[dict]:
        return self._range("samples", "ts, status, latency", lo, hi, hi_inclusive)

    def metrics_range(self, lo: int, hi: int, hi_inclusive: bool = True) -> list[dict]:
        return self._range(
            "metrics",
            "ts, proc_start, prompt_total, cached_total, predict_total, prompt_tps, predict_tps, busy",
            lo, hi, hi_inclusive,
        )

    def latest_sample(self) -> dict | None:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT ts, status, latency FROM samples ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return dict(r) if r is not None else None

    def latest_busy(self) -> int | None:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT busy FROM metrics ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return r["busy"] if r is not None else None
