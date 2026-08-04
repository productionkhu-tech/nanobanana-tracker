"""SQLite schema + helpers. Single file: app/db.sqlite (gitignored)."""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "db.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_daily (
  source        TEXT NOT NULL,    -- 'gcp' | 'openai' | 'byteplus'
  category      TEXT NOT NULL,    -- service description (gcp) or line_item (openai)
  date          TEXT NOT NULL,    -- 'YYYY-MM-DD' UTC
  request_count INTEGER,
  image_count   INTEGER,
  cost_usd      REAL NOT NULL DEFAULT 0,
  gross_cost    REAL,
  -- 청구 통화 원본 금액. GCP는 KRW로 청구되므로 그날 GCP가 적용한 환율이
  -- 이미 반영된 '실제 청구액'. USD로 바꿨다가 최신 환율로 되돌리면 과거
  -- 월 원화가 청구서와 어긋나므로 원본을 그대로 보관한다.
  -- OpenAI/BytePlus는 USD 청구라 NULL (표시할 때 최신 환율로 추정).
  cost_krw      REAL,
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

CREATE TABLE IF NOT EXISTS meta (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_ts INTEGER
);
"""


def set_meta(c: sqlite3.Connection, key: str, value: str) -> None:
    import time as _t
    c.execute(
        "INSERT INTO meta (key, value, updated_ts) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
        (key, value, int(_t.time())),
    )


def get_meta(c: sqlite3.Connection, key: str, default=None):
    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _migrate(c: sqlite3.Connection) -> None:
    """기존 DB에 새 컬럼 추가 (CREATE TABLE IF NOT EXISTS로는 안 붙음)."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(usage_daily)")}
    if "cost_krw" not in cols:
        c.execute("ALTER TABLE usage_daily ADD COLUMN cost_krw REAL")


@contextmanager
def conn():
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    try:
        c.executescript(SCHEMA)
        _migrate(c)
        yield c
        c.commit()
    finally:
        c.close()


def upsert_usage(c: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    for r in rows:
        r.setdefault("cost_krw", None)
    sql = """
    INSERT INTO usage_daily (source, category, date, request_count, image_count,
                             cost_usd, gross_cost, cost_krw)
    VALUES (:source, :category, :date, :request_count, :image_count,
            :cost_usd, :gross_cost, :cost_krw)
    ON CONFLICT(source, category, date) DO UPDATE SET
      request_count = excluded.request_count,
      image_count   = excluded.image_count,
      cost_usd      = excluded.cost_usd,
      gross_cost    = excluded.gross_cost,
      cost_krw      = excluded.cost_krw
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
