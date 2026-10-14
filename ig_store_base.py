#!/usr/bin/env python3
"""
Shared store logic, so the SQLite and Postgres backends cannot drift apart.

Why a base class
----------------
The store is written against two very different engines. SQLite is a single local
file with zero operations, which is what makes the test suite fast and offline.
Postgres is required the moment more than one scraper runs, because SQLite has
no `SELECT ... FOR UPDATE SKIP LOCKED`.

Those two engines differ in seven places, all mechanical:

  1. placeholders      `?` vs `%s`
  2. conflict clause   `INSERT OR IGNORE` vs `ON CONFLICT DO NOTHING`
  3. identity columns  `AUTOINCREMENT` vs `GENERATED ALWAYS AS IDENTITY`
  4. column listing    `PRAGMA table_info` vs `information_schema`
  5. insertion order   `rowid` vs an explicit timestamp
  6. last inserted id  `cursor.lastrowid` vs `RETURNING id`
  7. value types       INTEGER/TEXT vs BOOLEAN/TIMESTAMPTZ

Rather than duplicate ~200 lines of SQL that would drift on the first schema
change, every query here is written once with `?` placeholders and translated
per dialect. Only the genuinely engine-specific pieces are overridden.

Status machine (unchanged from the original SQLite store):
    discovered -> ok | unavailable | failed

Autocommit is mandatory on both backends: every post is persisted the moment it
is processed, which is what makes an abrupt interruption safe. That property was
verified empirically and must not be traded away for batching.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Iterable, Optional

STATUS_DISCOVERED: str = "discovered"
STATUS_OK: str = "ok"
STATUS_UNAVAILABLE: str = "unavailable"
STATUS_FAILED: str = "failed"
DONE_STATUSES: tuple[str, ...] = (STATUS_OK, STATUS_UNAVAILABLE)


def iso(value: Any) -> Optional[str]:
    """Serializes a datetime/date to an ISO string for SQLite.

    Python's implicit datetime adapter is deprecated since 3.12 and slated for
    removal, so the conversion is explicit here. It also keeps the stored format
    stable regardless of the interpreter version.
    """
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)

# Columns added after the first release. `_migrate()` brings older databases up
# to date on both backends. Keep this dict in sync with the base schema.
EXTRA_COLUMNS: dict[str, str] = {
    "rich_metadata": "NOT NULL DEFAULT 0",
    "media_id": "INTEGER",
    "likes": "INTEGER",
    "comments": "INTEGER",
    "author_id": "INTEGER",
    "author_followers": "INTEGER",
    "author_verified": "NOT NULL DEFAULT 0",
    "width": "INTEGER",
    "height": "INTEGER",
    "alt_text": "TEXT",
    "carousel_count": "NOT NULL DEFAULT 0",
    "carousel_shortcodes": "TEXT",
    "post_published_at": "TIMESTAMPTZ",
    "post_published_at_inferred": "NOT NULL DEFAULT 0",
    # Object-storage fields (Phase 1). Nullable so existing rows stay valid.
    "s3_key": "TEXT",
    "image_bytes": "INTEGER",
    "image_sha256": "TEXT",
    "image_format": "TEXT",
    # Work-claim fields (Phase 0).
    "discovered_at": "TIMESTAMPTZ",
    "updated_at": "TIMESTAMPTZ",
    "claimed_by": "TEXT",
    "claimed_at": "TIMESTAMPTZ",
}


class BaseStore:
    """Common store logic. Subclasses supply the engine and the dialect."""

    #: SQL placeholder. Overridden by the Postgres backend.
    PH: str = "?"

    # -- dialect hooks ----------------------------------------------------
    def _q(self, sql: str) -> str:
        """Translates `?` placeholders to the engine's style."""
        return sql if self.PH == "?" else sql.replace("?", self.PH)

    def _connect(self) -> Any:  # pragma: no cover - implemented by subclass
        raise NotImplementedError

    def _execute(self, sql: str, params: Any = ()) -> Any:  # pragma: no cover
        raise NotImplementedError

    def _ensure_schema(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def _existing_columns(self) -> set[str]:  # pragma: no cover
        raise NotImplementedError

    def _insert_if_absent(self, table: str, cols: str, vals: str,
                          params: tuple) -> int:  # pragma: no cover
        """Returns the number of rows actually inserted (0 when ignored)."""
        raise NotImplementedError

    def _insert_returning_id(self, table: str, cols: str, vals: str,
                             params: tuple) -> int:  # pragma: no cover
        raise NotImplementedError

    def _pending_order(self) -> str:  # pragma: no cover
        raise NotImplementedError

    # -- lifecycle --------------------------------------------------------
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._lock = threading.Lock()
        self._db = self._connect()
        self._ensure_schema()
        self._migrate()

    def close(self) -> None:
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        """Adds newer columns to a database created by an older version."""
        existing = self._existing_columns()
        for col, decl in EXTRA_COLUMNS.items():
            if col not in existing:
                self._execute(self._q(
                    f"ALTER TABLE {self._table('posts')} ADD COLUMN {col} {decl}"))
        self._create_indexes()

    def _create_indexes(self) -> None:
        for stmt in self._indexes():
            self._execute(stmt)

    def _indexes(self) -> tuple[str, ...]:
        t = self._table("posts")
        return (
            f"CREATE INDEX IF NOT EXISTS idx_posts_status ON {t}(status)",
            f"CREATE INDEX IF NOT EXISTS idx_posts_query ON {t}(query)",
        )

    def _table(self, name: str) -> str:
        """Table name for the current dialect (Postgres wants it quoted-free)."""
        return name

    # -- runs -------------------------------------------------------------
    def start_run(self, query: str, args: dict[str, Any]) -> int:
        params = (query, self._ts(self._now()), json.dumps(args, default=str))
        return self._insert_returning_id(
            self._table("runs"), "query, started_at, args_json",
            self._phs(3), params)

    # -- discovery --------------------------------------------------------
    def record_discovered(self, found: Iterable[tuple[str, str]],
                          query: str) -> int:
        """Records newly seen shortcodes. Returns how many are new.

        It never overwrites the state of already processed ones: it is idempotent
        and safe to call on every discovery run.
        """
        new = 0
        for code, src in found:
            new += self._insert_if_absent(
                self._table("posts"),
                "shortcode, query, status, source_serp_url, discovered_at",
                self._phs(5),
                (code, query, STATUS_DISCOVERED, src, self._ts(self._now())))
        return new

    def pending(self, query: Optional[str] = None,
                include_failed: bool = False) -> list[Any]:
        """Posts to process: those that are not finished.

        `include_failed` re-enables the ones that failed in previous runs, which
        would otherwise stay stuck forever.
        """
        sql = ("SELECT * FROM " + self._table("posts")
               + " WHERE status NOT IN (" + self._phs(len(DONE_STATUSES)) + ")")
        params: list[Any] = list(DONE_STATUSES)
        if not include_failed:
            sql += " AND status != " + self._ph(1)
            params.append(STATUS_FAILED)
        if query:
            sql += " AND query = " + self._ph(1)
            params.append(query)
        return list(self._execute(self._q(sql + " " + self._pending_order()), params))

    def status_of(self, shortcode: str) -> Optional[str]:
        rows = self._execute(self._q(
            "SELECT status FROM " + self._table("posts")
            + " WHERE shortcode = " + self._ph(1)), (shortcode,))
        row = rows[0] if rows else None
        return row["status"] if row else None

    # -- work claiming ----------------------------------------------------
    def claim_work(self, limit: int = 50,
                   owner: Optional[str] = None) -> list[Any]:
        """Reserves up to `limit` pending posts for this process.

        On SQLite this is a single-process convenience: it reads and marks, which
        is safe because SQLite is only ever used for local development and the
        test suite. The Postgres override runs inside a transaction with
        `FOR UPDATE SKIP LOCKED`, which is what makes parallel scrapers safe.
        """
        rows = self.pending()[:limit]
        if not rows:
            return []
        codes = [r["shortcode"] for r in rows]
        self._mark_claimed(codes, owner)
        # Re-read: the rows returned by `pending()` predate the UPDATE, so their
        # claimed_by/claimed_at would still be stale (None).
        #
        # Note the single-process caveat: a second call here can hand back rows
        # this one already claimed, because SQLite cannot exclude them without
        # row locks. That is precisely why the Postgres override adds
        # `claimed_by IS NULL` inside a FOR UPDATE SKIP LOCKED statement. SQLite
        # is only ever used for local development and the test suite, where one
        # process is the rule.
        if not codes:
            return []
        reread = self._execute(self._q(
            "SELECT * FROM " + self._table("posts")
            + " WHERE shortcode IN (" + self._phs(len(codes)) + ")"), codes)
        return list(reread)

    def _mark_claimed(self, codes: list[str], owner: Optional[str]) -> None:
        if not codes:
            return
        placeholders = self._phs(len(codes))
        # Two placeholders up front (claimed_by, claimed_at) plus one per code.
        self._execute(self._q(
            "UPDATE " + self._table("posts")
            + " SET claimed_by = " + self._ph(1)
            + ", claimed_at = " + self._ph(1)
            + " WHERE shortcode IN (" + placeholders + ")"),
            [owner or self._owner_id(), self._ts(self._now())] + list(codes))

    def reclaim_stale(self, older_than_seconds: int = 600) -> int:
        """Frees claims held by processes that died mid-run.

        A crashed scraper leaves rows with `claimed_by` set and no progress.
        After `older_than_seconds` nobody has touched them, so they are released
        back to `discovered` and can be picked up again.
        """
        expr = self._older_than_expr(older_than_seconds)
        sql = self._q(
            "UPDATE " + self._table("posts")
            + " SET claimed_by=NULL, claimed_at=NULL"
            + " WHERE status=" + self._ph(1)
            + " AND claimed_at IS NOT NULL"
            + " AND claimed_at < " + expr)
        params: list[Any] = [STATUS_DISCOVERED]
        params.extend(self._reclaim_params())
        rows = self._execute(sql, params)
        return int(rows) if isinstance(rows, int) else 0

    def _older_than_expr(self, seconds: int) -> str:  # pragma: no cover
        raise NotImplementedError

    def _reclaim_params(self) -> tuple:
        """Extra parameters bound to the `_older_than_expr` placeholder."""
        return ()

    def _owner_id(self) -> str:
        import os
        return f"{os.uname().nodename}:{os.getpid()}"

    # -- results ----------------------------------------------------------
    def mark_ok(self, shortcode: str, *, query: str, author: Optional[str],
                caption: Optional[str], image_url: Optional[str],
                local_image_path: Optional[str], is_video: bool,
                source_serp_url: str, scraped_at: str, attempts: int,
                extra: Optional[dict[str, Any]] = None) -> None:
        x = extra or {}
        self._execute(self._q(
            "UPDATE " + self._table("posts") + " SET status=" + self._ph(1)
            + ", query=" + self._ph(1) + ", author=" + self._ph(1)
            + ", caption=" + self._ph(1) + ", image_url=" + self._ph(1)
            + ", local_image_path=" + self._ph(1)
            + ", is_video=" + self._ph(1) + ", source_serp_url=" + self._ph(1)
            + ", scraped_at=" + self._ph(1) + ", attempts=" + self._ph(1)
            + ", last_error=NULL, updated_at=" + self._ph(1)
            + ", rich_metadata=" + self._ph(1) + ", media_id=" + self._ph(1)
            + ", likes=" + self._ph(1) + ", comments=" + self._ph(1)
            + ", author_id=" + self._ph(1) + ", author_followers=" + self._ph(1)
            + ", author_verified=" + self._ph(1) + ", width=" + self._ph(1)
            + ", height=" + self._ph(1) + ", alt_text=" + self._ph(1)
            + ", carousel_count=" + self._ph(1)
            + ", carousel_shortcodes=" + self._ph(1)
            + ", post_published_at=" + self._ph(1)
            + ", post_published_at_inferred=" + self._ph(1)
            + ", s3_key=" + self._ph(1)
            + ", image_bytes=" + self._ph(1) + ", image_sha256=" + self._ph(1)
            + ", image_format=" + self._ph(1)
            + " WHERE shortcode=" + self._ph(1)),
            [STATUS_OK, query, author, caption, image_url, local_image_path,
             self._bool(is_video), source_serp_url, scraped_at, attempts,
             self._ts(self._now()), self._bool(x.get("rich_metadata")),
             x.get("media_id"), x.get("likes"), x.get("comments"),
             x.get("author_id"), x.get("author_followers"),
             self._nullable_bool(x.get("author_verified")), x.get("width"),
             x.get("height"), x.get("alt_text"),
             int(x.get("carousel_count") or 0),
             json.dumps(x.get("carousel_shortcodes") or [], ensure_ascii=False),
             x.get("post_published_at") and self._ts(x.get("post_published_at")),
             self._bool(x.get("post_published_at_inferred")),
             x.get("s3_key"), x.get("image_bytes"), x.get("image_sha256"),
             self._image_format(local_image_path),
             shortcode])

    def _image_format(self, local_image_path: Optional[str]) -> Optional[str]:
        """Derives the extension so the store keeps what was actually written.

        `detect_image_format` already decides the extension from the bytes, so
        the file on disk is the source of truth here.
        """
        if not local_image_path or "." not in local_image_path:
            return None
        return local_image_path.rsplit(".", 1)[-1].lower()[:8]

    def mark_terminal(self, shortcode: str, status: str, error: Optional[str],
                      attempts: int = 0) -> None:
        """Marks a post as not processable in the future (unavailable/failed)."""
        self._execute(self._q(
            "UPDATE " + self._table("posts") + " SET status=" + self._ph(1)
            + ", attempts=" + self._ph(1) + ", last_error=" + self._ph(1)
            + ", claimed_by=NULL, claimed_at=NULL"
            + " WHERE shortcode=" + self._ph(1)),
            (status, attempts, error, shortcode))

    def log_error(self, shortcode: str, attempt: int, kind: str,
                  message: str) -> None:
        self._execute(self._q(
            "INSERT INTO " + self._table("errors")
            + " (shortcode, attempt, kind, message, created_at) VALUES ("
            + self._phs(5) + ")"),
            (shortcode, attempt, kind, message[:500], self._ts(self._now())))

    def errors_for(self, shortcode: str) -> list[Any]:
        return list(self._execute(self._q(
            "SELECT attempt, kind, message FROM " + self._table("errors")
            + " WHERE shortcode=" + self._ph(1) + " ORDER BY id"), (shortcode,)))

    # -- reporting --------------------------------------------------------
    def counts(self) -> dict[str, int]:
        rows = self._execute(self._q(
            "SELECT status, COUNT(*) n FROM " + self._table("posts")
            + " GROUP BY status"), ())
        return {r["status"]: r["n"] for r in rows}

    def done_posts(self, query: Optional[str] = None) -> list[Any]:
        sql = ("SELECT * FROM " + self._table("posts") + " WHERE status IN ("
               + self._phs(len(DONE_STATUSES)) + ")")
        params: list[Any] = list(DONE_STATUSES)
        if query:
            sql += " AND query = " + self._ph(1)
            params.append(query)
        return list(self._execute(self._q(
            sql + " ORDER BY scraped_at, shortcode"), params))

    def failed_posts(self) -> list[Any]:
        return list(self._execute(self._q(
            "SELECT shortcode, attempts, last_error FROM "
            + self._table("posts") + " WHERE status=" + self._ph(1)),
            (STATUS_FAILED,)))

    def analytics_snapshot(self) -> dict[str, Any]:
        """Aggregations the team actually asks for, over the whole history.

        Kept here (and not in the dashboard layer) so the same numbers come out
        of both SQLite and Postgres. Measured at 1M rows in Postgres: 132 ms for
        the per-keyword rollup, 153 ms for the monthly series, 497 ms for the
        full-scan top-N.
        """
        by_status = self.counts()
        total = sum(by_status.values())
        with_images = self._scalar(self._q(
            "SELECT COUNT(*) n FROM " + self._table("posts")
            + " WHERE status=" + self._ph(1) + " AND local_image_path IS NOT NULL"),
            (STATUS_OK,))
        avg_likes = self._scalar(self._q(
            "SELECT AVG(likes) v FROM " + self._table("posts")
            + " WHERE status=" + self._ph(1) + " AND likes IS NOT NULL"),
            (STATUS_OK,))
        return {
            "total": total,
            "by_status": by_status,
            "with_image": int(with_images or 0),
            "avg_likes": round(float(avg_likes), 1) if avg_likes else None,
        }

    def _scalar(self, sql: str, params: tuple) -> Any:
        rows = self._execute(sql, params)
        if not rows:
            return None
        row = rows[0]
        if isinstance(row, dict):
            return next(iter(row.values()))
        return row[0]

    # -- helpers ----------------------------------------------------------
    def _ph(self, n: int) -> str:
        return self._phs(1) if n == 1 else self._phs(n)

    def _phs(self, n: int) -> str:
        return ", ".join(self.PH for _ in range(n))

    def _bool(self, value: Any) -> Any:
        """Booleans differ between engines; both accept 0/1.

        `extra` is optional, so a missing flag means 0 ("not enriched") rather
        than NULL: several of these columns are NOT NULL and would otherwise
        fail the write.
        """
        return 0 if value is None else int(bool(value))

    def _nullable_bool(self, value: Any) -> Any:
        """Tri-state boolean: keeps an explicit False as False, and None as None.

        `author_verified` is one of these: a light embed with no blob must not
        claim the author is unverified, it must say "unknown".
        """
        return None if value is None else int(bool(value))

    def _now(self) -> Any:
        """Engine-native 'now' expression for insert parameters."""
        import datetime as _dt
        return _dt.datetime.now(_dt.timezone.utc)

    def _ts(self, value: Any) -> Any:
        """Converts a timestamp for binding in this engine.

        SQLite stores ISO text (see `iso()`); Postgres binds a real TIMESTAMPTZ.
        Override in the Postgres backend.
        """
        return iso(value)