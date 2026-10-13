#!/usr/bin/env python3
"""
Incremental SQLite store for the scrapers.

Why it exists: `metadata.json` used to be written only once, at the end of the
loop. On a long run —thousands of posts— an interruption halfway through threw
everything away: the images stayed on disk and the metadata (author, caption,
URL) was lost. Each post is persisted as soon as it is processed, so the run is
resumable.

  posts(shortcode PK, query, status, author, caption, image_url,
        local_image_path, is_video, source_serp_url, scraped_at,
        attempts, last_error, ...)
  status: discovered -> ok | unavailable | failed
  errors(id, shortcode, attempt, kind, message, created_at)
  runs(run_id, query, started_at, args_json)

A run is a separate `run_id` so it can be reprocessed without losing history.

WHEN TO USE THIS BACKEND
------------------------
SQLite is the right choice for local development and for the test suite: zero
operations (a single file), instant startup, and no server to keep alive.

It is the WRONG choice for more than one concurrent scraper. `claim_work()`
here is single-process only, because SQLite has no `SELECT ... FOR UPDATE
SKIP LOCKED` — two processes reading `pending()` get the same rows and redo the
same work (measured: 80% duplication with five processes). Use
`ig_postgres_store.PostgresStore` for anything beyond a single scraper.

All shared logic lives in `ig_store_base.BaseStore`; this module only supplies
the engine and the SQLite dialect.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Final, Optional

from ig_store_base import (  # noqa: F401  (re-exported for existing importers)
    DONE_STATUSES,
    STATUS_DISCOVERED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    BaseStore,
)

SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS posts (
    shortcode        TEXT PRIMARY KEY,
    query            TEXT,
    status           TEXT NOT NULL DEFAULT 'discovered',
    author           TEXT,
    caption          TEXT,
    image_url        TEXT,
    local_image_path TEXT,
    is_video         INTEGER NOT NULL DEFAULT 0,
    source_serp_url  TEXT,
    scraped_at       TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    -- rich metadata (embed GraphQL blob); null when the embed was light
    rich_metadata      INTEGER NOT NULL DEFAULT 0,
    media_id           INTEGER,
    likes              INTEGER,
    comments           INTEGER,
    author_id          INTEGER,
    author_followers   INTEGER,
    author_verified    INTEGER,
    width              INTEGER,
    height             INTEGER,
    alt_text           TEXT,
    carousel_count     INTEGER NOT NULL DEFAULT 0,
    carousel_shortcodes TEXT,
    post_published_at  TEXT,
    post_published_at_inferred INTEGER NOT NULL DEFAULT 0,
    -- object storage (Phase 1)
    s3_key          TEXT,
    image_bytes     INTEGER,
    image_sha256    TEXT,
    image_format    TEXT,
    -- work claiming (Phase 0)
    discovered_at   TEXT,
    updated_at      TEXT,
    claimed_by      TEXT,
    claimed_at      TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    shortcode  TEXT NOT NULL,
    attempt    INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    message    TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    query      TEXT,
    started_at REAL NOT NULL,
    args_json  TEXT
);
"""


class PostStore(BaseStore):
    """SQLite-backed store. All SQL is local; nothing to operate."""

    PH = "?"

    def _connect(self) -> Any:
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None = autocommit: every write is already on disk,
        # which is exactly what makes an abrupt interruption safe.
        # check_same_thread=False + lock: phase 2/3 can run with several
        # workers, and a shared SQLite connection without serializing the
        # writes corrupts the database.
        db = sqlite3.connect(str(path), isolation_level=None,
                             check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def _execute(self, sql: str, params: Any = ()) -> Any:
        with self._lock:
            return self._db.execute(sql, params)

    def _ex(self, sql: str, params: Any = ()) -> Any:
        """Backwards-compatible escape hatch used by the test suite.

        Callers must use `?` placeholders; they are translated here so the same
        query works on either backend.
        """
        return self._execute(self._q(sql), params)

    def _ensure_schema(self) -> None:
        self._db.executescript(SCHEMA)

    def _existing_columns(self) -> set[str]:
        return {r[1] for r in self._db.execute("PRAGMA table_info(posts)")}

    def _indexes(self) -> tuple[str, ...]:
        return super()._indexes() + (
            "CREATE INDEX IF NOT EXISTS idx_errors_shortcode"
            " ON errors(shortcode)",
        )

    def _insert_if_absent(self, table: str, cols: str, vals: str,
                          params: tuple) -> int:
        cur = self._execute(
            f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({vals})", params)
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def _insert_returning_id(self, table: str, cols: str, vals: str,
                             params: tuple) -> int:
        cur = self._execute(
            f"INSERT INTO {table} ({cols}) VALUES ({vals})", params)
        return int(cur.lastrowid or 0)

    def _pending_order(self) -> str:
        return "ORDER BY rowid"

    def _older_than_expr(self, seconds: int) -> str:
        # SQLite has no interval arithmetic, so the cut-off is computed here and
        # bound as a parameter by `reclaim_stale`.
        import datetime as _dt
        self._stale_cutoff = (_dt.datetime.now(_dt.timezone.utc)
                              - _dt.timedelta(seconds=seconds)).isoformat()
        return self._ph(1)

    def _reclaim_params(self) -> tuple:
        return (getattr(self, "_stale_cutoff", None),)

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        super().__init__()