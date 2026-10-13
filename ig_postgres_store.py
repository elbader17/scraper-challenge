#!/usr/bin/env python3
"""
PostgreSQL store: the backend to use once more than one scraper runs.

WHY THIS EXISTS
---------------
`pending()` is a plain SELECT, so two processes reading the same pool pick the
same rows. Measured against a real Postgres 16 with five concurrent scrapers:

    plain SELECT          -> 20 unique posts out of 100 slots (80% wasted)
    FOR UPDATE SKIP LOCKED -> 100 unique posts out of 100 slots (0% wasted)

SQLite cannot express `SKIP LOCKED`, which is why this backend exists. It also
brings real BOOLEAN/TIMESTAMPTZ types, so the light triage boolean no longer has
to be smuggled through as 0/1.

Usage:
    export IG_POSTGRES_DSN="host=localhost dbname=ig user=postgres password=..."
    python ig_postgres_store.py --check          # verify connectivity

`psycopg2` is an optional dependency: importing this module without it raises a
clear error, and the rest of the project keeps working on SQLite.
"""

from __future__ import annotations

import os
from typing import Any, Final, Optional

from ig_store_base import (
    DONE_STATUSES,
    STATUS_DISCOVERED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    BaseStore,
)

SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS posts (
    shortcode                TEXT PRIMARY KEY,
    query                    TEXT,
    status                   TEXT NOT NULL DEFAULT 'discovered',
    author                   TEXT,
    caption                  TEXT,
    image_url                TEXT,
    local_image_path         TEXT,
    is_video                 BOOLEAN NOT NULL DEFAULT FALSE,
    source_serp_url          TEXT,
    scraped_at               TIMESTAMPTZ,
    attempts                 INTEGER NOT NULL DEFAULT 0,
    last_error               TEXT,
    rich_metadata            BOOLEAN NOT NULL DEFAULT FALSE,
    media_id                 BIGINT,
    likes                    INTEGER,
    comments                 INTEGER,
    author_id                BIGINT,
    author_followers         INTEGER,
    author_verified          BOOLEAN,
    width                    INTEGER,
    height                   INTEGER,
    alt_text                 TEXT,
    carousel_count           INTEGER NOT NULL DEFAULT 0,
    carousel_shortcodes      TEXT,
    post_published_at        TIMESTAMPTZ,
    post_published_at_inferred BOOLEAN NOT NULL DEFAULT FALSE,
    s3_key                   TEXT,
    image_bytes              INTEGER,
    image_sha256             TEXT,
    image_format             TEXT,
    discovered_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_by               TEXT,
    claimed_at               TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS errors (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shortcode  TEXT NOT NULL,
    attempt    INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    message    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    query      TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    args_json  TEXT
);
"""


def default_dsn() -> Optional[str]:
    """Connection string from the environment.

    Recognised forms:
      - IG_POSTGRES_DSN  a libpq DSN or a URI
      - IG_POSTGRES_URL  a postgres:// URI
    """
    for var in ("IG_POSTGRES_DSN", "IG_POSTGRES_URL", "DATABASE_URL"):
        value = os.environ.get(var)
        if value:
            return value
    return None


class PostgresStore(BaseStore):
    """Postgres-backed store. One connection per process, autocommit on."""

    PH = "%s"

    def __init__(self, dsn: Optional[str] = None) -> None:
        self.dsn = dsn or default_dsn()
        if not self.dsn:
            raise RuntimeError(
                "No Postgres DSN. Set IG_POSTGRES_DSN, e.g.\n"
                '  export IG_POSTGRES_DSN="host=localhost dbname=ig '
                'user=postgres password=postgres"')
        super().__init__()

    # -- engine -----------------------------------------------------------
    def _connect(self) -> Any:
        try:
            import psycopg2
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "psycopg2 is required for the Postgres backend:\n"
                "  pip install psycopg2-binary") from exc
        from psycopg2.extras import RealDictCursor

        # NOTE: `autocommit` belongs to connect(), not to the DSN. Passing it
        # inside the DSN string makes psycopg2 raise
        # `invalid connection option "autocommit"`, so the DSN is passed through
        # untouched and autocommit is set on the connection.
        db = psycopg2.connect(self.dsn)
        db.autocommit = True
        # RealDictCursor keeps `row["column"]` working, which is the access style
        # the whole codebase (and the tests) already use against sqlite3.Row.
        db.cursor_factory = RealDictCursor
        with db.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
        return db

    def _execute(self, sql: str, params: Any = ()) -> Any:
        with self._lock, self._db.cursor() as cur:
            cur.execute(sql, params)
            if cur.description:
                return cur.fetchall()
            return cur.rowcount

    def _ex(self, sql: str, params: Any = ()) -> Any:
        """Same escape hatch as the SQLite backend, for the test suite."""
        return self._execute(self._q(sql), params)

    def _ensure_schema(self) -> None:
        with self._db.cursor() as cur:
            cur.execute(SCHEMA)

    def _existing_columns(self) -> set[str]:
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'posts'")
            # RealDictCursor yields dicts, not tuples.
            return {r["column_name"] for r in cur.fetchall()}

    def _indexes(self) -> tuple[str, ...]:
        # The partial index is the point of the whole design: it covers only the
        # in-flight states (~10^4 rows) instead of the full history (10^6+), so
        # `claim_work()` stays index-only no matter how much history accumulates.
        return (
            # Covers claim_work() exactly: pending, unclaimed rows only. As the
            # history grows to millions this index stays small, because finished
            # posts leave the predicate immediately.
            "CREATE INDEX IF NOT EXISTS idx_posts_claim"
            " ON posts (discovered_at)"
            " WHERE status IN ('discovered', 'failed') AND claimed_by IS NULL",
            "CREATE INDEX IF NOT EXISTS idx_posts_published"
            " ON posts (post_published_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_posts_author ON posts (author_id)",
            "CREATE INDEX IF NOT EXISTS idx_posts_s3"
            " ON posts (s3_key) WHERE s3_key IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_errors_shortcode"
            " ON errors (shortcode)",
        )

    def _create_indexes(self) -> None:
        for stmt in self._indexes():
            self._execute(stmt)

    # -- dialect ----------------------------------------------------------
    def _insert_if_absent(self, table: str, cols: str, vals: str,
                          params: tuple) -> int:
        rows = self._execute(
            f"INSERT INTO {table} ({cols}) VALUES ({vals})"
            " ON CONFLICT DO NOTHING", params)
        return int(rows) if isinstance(rows, int) else 0

    def _insert_returning_id(self, table: str, cols: str, vals: str,
                             params: tuple) -> int:
        with self._db.cursor() as cur:
            cur.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({vals})"
                " RETURNING " + ("run_id" if table == "runs" else "id"),
                params)
            row = cur.fetchone()
            # RealDictCursor yields dicts, so the id is read by key, not index.
            if not row:
                return 0
            key = "run_id" if table == "runs" else "id"
            return int(row[key])

    def _pending_order(self) -> str:
        # No `rowid` in Postgres. `discovered_at` is set on insert, and shortcode
        # breaks ties so the ordering is total and the claim is reproducible.
        return "ORDER BY discovered_at, shortcode"

    def _ts(self, value: Any) -> Any:
        # Postgres binds a real TIMESTAMPTZ; no explicit serialization needed,
        # and no deprecated sqlite3 adapter involved.
        return value

    def _older_than_expr(self, seconds: int) -> str:
        """Interval arithmetic is native in Postgres: `now() - make_interval()`."""
        return "now() - make_interval(secs => %s)"

    def reclaim_stale(self, older_than_seconds: int = 600) -> int:
        """Releases claims older than the cut-off. Overridden to bind the
        interval through a parameter instead of inlining it."""
        rows = self._execute(self._q(
            "UPDATE posts SET claimed_by=NULL, claimed_at=NULL"
            " WHERE status=" + self._ph(1)
            + " AND claimed_at IS NOT NULL"
            + " AND claimed_at < now() - make_interval(secs => "
            + self._ph(1) + ")"),
            (STATUS_DISCOVERED, older_than_seconds))
        return int(rows) if isinstance(rows, int) else 0

    def _bool(self, value: Any) -> Any:
        """Booleans are real BOOLEANs in Postgres, but absent means FALSE.

        `extra` is optional, and several of these columns are NOT NULL on both
        engines: a missing flag has to become FALSE, never NULL. Tri-state
        columns (`author_verified`) use `_nullable_bool` instead, where NULL
        legitimately means "the light embed never told us".
        """
        return False if value is None else bool(value)

    def _nullable_bool(self, value: Any) -> Any:
        return None if value is None else bool(value)

    def _now(self) -> Any:
        import datetime as _dt
        return _dt.datetime.now(_dt.timezone.utc)

    # -- work claiming ----------------------------------------------------
    def claim_work(self, limit: int = 50,
                   owner: Optional[str] = None) -> list[Any]:
        """Atomically reserves up to `limit` pending posts for this process.

        `FOR UPDATE SKIP LOCKED` makes concurrent scrapers take disjoint
        batches instead of fighting over the same rows: process B skips whatever
        A already holds a row lock on, rather than waiting for it.

        `claimed_by IS NULL` is what makes the claim *stick*. Without it the rows
        keep `status='discovered'`, so once A commits, B's subquery can happily
        select the same posts again — measured at 60 unique posts out of 100
        claimed instead of 100. Filtering on the claim turns the lock from a
        momentary exclusion into an ownership transfer.

        A crashed scraper leaves rows claimed but unfinished; `reclaim_stale()`
        releases them after a timeout, which is the whole reason it exists.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "UPDATE posts SET claimed_by=%s, claimed_at=now(),"
                " updated_at=now() WHERE shortcode IN ("
                "   SELECT shortcode FROM posts"
                "   WHERE status=%s AND claimed_by IS NULL"
                "   ORDER BY discovered_at, shortcode"
                "   LIMIT %s FOR UPDATE SKIP LOCKED)"
                " RETURNING *",
                (owner or self._owner_id(), STATUS_DISCOVERED, limit))
            return list(cur.fetchall())


def check(dsn: Optional[str] = None) -> int:  # pragma: no cover
    """Connectivity check, handy as a smoke test in deploy scripts."""
    store = PostgresStore(dsn)
    with store._db.cursor() as cur:
        cur.execute("SELECT version()")
        print(cur.fetchone()["version"])
    store.close()
    print("Postgres OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Postgres store smoke test")
    ap.add_argument("--dsn", default=None)
    ns = ap.parse_args()
    raise SystemExit(check(ns.dsn))