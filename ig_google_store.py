#!/usr/bin/env python3
"""
Almacén incremental (SQLite) para el scraper de Google.

Por qué existe: `metadata.json` se escribía una sola vez, al final del bucle.
En una corrida larga —miles de posts— un corte a mitad de camino tiraba todo:
las imágenes quedaban en disco y los metadatos (autor, caption, URL) se perdían.
Cada post se persiste apenas se procesa, así que la corrida es reanudable.

Esquema:
  posts(shortcode PK, query, status, author, caption, image_url,
        local_image_path, is_video, source_serp_url, scraped_at,
        attempts, last_error)
    status: discovered -> ok | unavailable | failed
  errors(id, shortcode, attempt, kind, message, created_at)

Una corrida es un `run_id` aparte para poder reprocesar sin perder el histórico.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Final, Iterable, Optional

STATUS_DISCOVERED: Final[str] = "discovered"
STATUS_OK: Final[str] = "ok"
STATUS_UNAVAILABLE: Final[str] = "unavailable"
STATUS_FAILED: Final[str] = "failed"
DONE_STATUSES: Final[tuple[str, ...]] = (STATUS_OK, STATUS_UNAVAILABLE)

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
    -- metadata enriquecida (blob GraphQL del embed); null si vino embed liviano
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
    post_published_at_inferred INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_query  ON posts(query);

CREATE TABLE IF NOT EXISTS errors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    shortcode  TEXT NOT NULL,
    attempt    INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    message    TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_errors_shortcode ON errors(shortcode);

CREATE TABLE IF NOT EXISTS runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    query      TEXT,
    started_at REAL NOT NULL,
    args_json  TEXT
);
"""


class PostStore:
    """Wrapper fino sobre SQLite. Todas las escrituras son inmediatas."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None = autocommit: cada write ya queda en disco, que
        # es justamente lo que hace segura una interrupción abrupta.
        # check_same_thread=False + lock: la fase 2/3 puede correr con varios
        # workers, y una conexión SQLite compartida sin serializar las escrituras
        # corrompe la base.
        self._db = sqlite3.connect(str(self.path), isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._migrate()

    def _ex(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "PostStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        """Agrega columnas nuevas a bases creadas por versiones anteriores."""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(posts)")}
        nuevos = {
            "rich_metadata": "INTEGER NOT NULL DEFAULT 0",
            "media_id": "INTEGER", "likes": "INTEGER", "comments": "INTEGER",
            "author_id": "INTEGER", "author_followers": "INTEGER",
            "author_verified": "INTEGER", "width": "INTEGER", "height": "INTEGER",
            "alt_text": "TEXT", "carousel_count": "INTEGER NOT NULL DEFAULT 0",
            "carousel_shortcodes": "TEXT",
            "post_published_at": "TEXT",
            "post_published_at_inferred": "INTEGER NOT NULL DEFAULT 0",
        }
        for col, decl in nuevos.items():
            if col not in cols:
                self._db.execute(f"ALTER TABLE posts ADD COLUMN {col} {decl}")

    # -- runs -------------------------------------------------------------
    def start_run(self, query: str, args: dict[str, Any]) -> int:
        cur = self._ex(
            "INSERT INTO runs (query, started_at, args_json) VALUES (?,?,?)",
            (query, time.time(), json.dumps(args, default=str)),
        )
        return int(cur.lastrowid or 0)

    # -- discovery --------------------------------------------------------
    def record_discovered(self, found: Iterable[tuple[str, str]], query: str) -> int:
        """Registra shortcodes recién vistos. Devuelve cuántos son nuevos.

        No pisa el estado de los ya procesados: es idempotente y se puede
        llamar en cada corrida de discovery.
        """
        new = 0
        for code, src in found:
            cur = self._ex(
                "INSERT OR IGNORE INTO posts (shortcode, query, status, source_serp_url)"
                " VALUES (?,?,?,?)",
                (code, query, STATUS_DISCOVERED, src),
            )
            new += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return new

    def pending(self, query: Optional[str] = None,
                include_failed: bool = False) -> list[sqlite3.Row]:
        """Posts a procesar: los que no están terminados.

        `include_failed` rehabilita los que fallaron en corridas previas, que de
        otro modo quedarían enganchados para siempre.
        """
        sql = "SELECT * FROM posts WHERE status NOT IN (%s)" % ",".join(
            "?" * len(DONE_STATUSES)
        )
        params: list[Any] = list(DONE_STATUSES)
        if not include_failed:
            sql += " AND status != ?"
            params.append(STATUS_FAILED)
        if query:
            sql += " AND query = ?"
            params.append(query)
        return list(self._ex(sql + " ORDER BY rowid", params))

    def status_of(self, shortcode: str) -> Optional[str]:
        row = self._ex(
            "SELECT status FROM posts WHERE shortcode = ?", (shortcode,)
        ).fetchone()
        return row["status"] if row else None

    # -- results ----------------------------------------------------------
    def mark_ok(self, shortcode: str, *, query: str, author: Optional[str],
                caption: Optional[str], image_url: Optional[str],
                local_image_path: Optional[str], is_video: bool,
                source_serp_url: str, scraped_at: str, attempts: int,
                extra: Optional[dict[str, Any]] = None) -> None:
        x = extra or {}
        self._ex(
            "UPDATE posts SET status=?, query=?, author=?, caption=?, image_url=?,"
            " local_image_path=?, is_video=?, source_serp_url=?, scraped_at=?,"
            " attempts=?, last_error=NULL,"
            " rich_metadata=?, media_id=?, likes=?, comments=?, author_id=?,"
            " author_followers=?, author_verified=?, width=?, height=?, alt_text=?,"
            " carousel_count=?, carousel_shortcodes=?, post_published_at=?,"
            " post_published_at_inferred=? WHERE shortcode=?",
            (STATUS_OK, query, author, caption, image_url, local_image_path,
             int(is_video), source_serp_url, scraped_at, attempts,
             int(bool(x.get("rich_metadata"))), x.get("media_id"), x.get("likes"),
             x.get("comments"), x.get("author_id"), x.get("author_followers"),
             None if x.get("author_verified") is None else int(x["author_verified"]),
             x.get("width"), x.get("height"), x.get("alt_text"),
             int(x.get("carousel_count") or 0),
             json.dumps(x.get("carousel_shortcodes") or [], ensure_ascii=False),
             x.get("post_published_at"),
             int(bool(x.get("post_published_at_inferred"))), shortcode),
        )

    def mark_terminal(self, shortcode: str, status: str, error: Optional[str],
                      attempts: int = 0) -> None:
        """Marca un post como no procesable en el futuro (unavailable/failed)."""
        self._ex(
            "UPDATE posts SET status=?, attempts=?, last_error=? WHERE shortcode=?",
            (status, attempts, error, shortcode),
        )

    def log_error(self, shortcode: str, attempt: int, kind: str, message: str) -> None:
        self._ex(
            "INSERT INTO errors (shortcode, attempt, kind, message, created_at)"
            " VALUES (?,?,?,?,?)",
            (shortcode, attempt, kind, message[:500], time.time()),
        )

    def errors_for(self, shortcode: str) -> list[sqlite3.Row]:
        return list(self._ex(
            "SELECT attempt, kind, message FROM errors WHERE shortcode=? ORDER BY id",
            (shortcode,),
        ))

    # -- reporting --------------------------------------------------------
    def counts(self) -> dict[str, int]:
        rows = self._ex("SELECT status, COUNT(*) n FROM posts GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def done_posts(self, query: Optional[str] = None) -> list[sqlite3.Row]:
        sql = ("SELECT * FROM posts WHERE status IN (%s)"
               % ",".join("?" * len(DONE_STATUSES)))
        params: list[Any] = list(DONE_STATUSES)
        if query:
            sql += " AND query = ?"
            params.append(query)
        return list(self._ex(sql + " ORDER BY scraped_at", params))

    def failed_posts(self) -> list[sqlite3.Row]:
        return list(self._ex(
            "SELECT shortcode, attempts, last_error FROM posts WHERE status=?",
            (STATUS_FAILED,),
        ))