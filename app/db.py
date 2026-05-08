"""SQLite schema + helpers. Single file: app/db.sqlite (gitignored)."""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "db.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_daily (
  source        TEXT NOT NULL,    -- 'gcp' | 'openai'
  category      TEXT NOT NULL,    -- service description (gcp) or line_item (openai)
  date          TEXT NOT NULL,    -- 'YYYY-MM-DD' UTC
  request_count INTEGER,
  image_count   INTEGER,
  cost_usd      REAL NOT NULL DEFAULT 0,
  gross_cost    REAL,
  PRIMARY KEY (source, category, date)
);

CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_daily(date);
CREATE INDEX IF NOT EXISTS idx_usage_source ON usage_daily(source);

CREATE TABLE IF NOT EXISTS sync_log (
  source       TEXT PRIMARY KEY,
  last_run_ts  INTEGER NOT NULL,
  last_data_ts INTEGER,
  status       TEXT NOT NULL,    -- 'ok' | 'error'
  error_msg    TEXT
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    try:
        c.executescript(SCHEMA)
        yield c
        c.commit()
    finally:
        c.close()


def upsert_usage(c: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    sql = """
    INSERT INTO usage_daily (source, category, date, request_count, image_count, cost_usd, gross_cost)
    VALUES (:source, :category, :date, :request_count, :image_count, :cost_usd, :gross_cost)
    ON CONFLICT(source, category, date) DO UPDATE SET
      request_count = excluded.request_count,
      image_count   = excluded.image_count,
      cost_usd      = excluded.cost_usd,
      gross_cost    = excluded.gross_cost
    """
    c.executemany(sql, rows)
    return len(rows)


def update_sync(c: sqlite3.Connection, source: str, run_ts: int, data_ts: int | None,
                status: str, err: str | None = None) -> None:
    c.execute(
        """INSERT INTO sync_log (source, last_run_ts, last_data_ts, status, error_msg)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET
             last_run_ts = excluded.last_run_ts,
             last_data_ts = COALESCE(excluded.last_data_ts, sync_log.last_data_ts),
             status = excluded.status,
             error_msg = excluded.error_msg""",
        (source, run_ts, data_ts, status, err),
    )
