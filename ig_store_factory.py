#!/usr/bin/env python3
"""
Chooses the store backend: SQLite for local work, Postgres for parallel runs.

The two backends share all logic through `ig_store_base.BaseStore`, so this is
only about picking the right engine for the situation:

    SQLite  - zero operations, one file, instant startup. Right for development
              and for the test suite, which must stay offline.
    Postgres - required once more than one scraper runs, because SQLite has no
              `SELECT ... FOR UPDATE SKIP LOCKED` and parallel processes would
              duplicate 80% of their work.

Selection order:
    1. explicit `backend="postgres"` / `backend="sqlite"`
    2. a postgres URI as `path` (e.g. "postgresql://user:pw@host/db")
    3. IG_POSTGRES_DSN in the environment
    4. SQLite (the default, so nothing breaks for existing callers)
"""

from __future__ import annotations

import os
from typing import Any, Optional

from ig_google_store import PostStore

POSTGRES_URI_PREFIXES = ("postgres://", "postgresql://")


def looks_like_postgres_uri(value: str) -> bool:
    return str(value).startswith(POSTGRES_URI_PREFIXES)


def postgres_dsn() -> Optional[str]:
    """DSN from the environment, if one is configured."""
    for var in ("IG_POSTGRES_DSN", "IG_POSTGRES_URL", "DATABASE_URL"):
        value = os.environ.get(var)
        if value:
            return value
    return None


def make_store(path: Optional[str] = None, backend: Optional[str] = None,
               **kwargs: Any) -> Any:
    """Returns a store for the requested backend.

    `path` may be a filesystem path (SQLite) or a postgres URI. Passing
    `backend="postgres"` forces Postgres even for a bare path.
    """
    if backend is None:
        if path and looks_like_postgres_uri(path):
            backend = "postgres"
        elif postgres_dsn():
            backend = "postgres"
        else:
            backend = "sqlite"

    if backend == "postgres":
        from ig_postgres_store import PostgresStore

        dsn = kwargs.pop("dsn", None)
        if path and looks_like_postgres_uri(path):
            dsn = dsn or path
        return PostgresStore(dsn=dsn, **kwargs)

    if backend == "sqlite":
        if not path:
            raise ValueError("SQLite store needs a file path")
        return PostStore(path, **kwargs)

    raise ValueError(f"unknown backend: {backend!r} (use 'sqlite' or 'postgres')")