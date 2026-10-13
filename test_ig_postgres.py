#!/usr/bin/env python3
"""
Tests for the Postgres backend. Skipped cleanly when no server is reachable.

    # with a server running:
    export IG_POSTGRES_DSN="host=localhost port=5433 dbname=ig user=postgres password=p"
    python test_ig_postgres.py

    # without one: every check reports SKIP and the suite exits 0.

The important test here is `claim_work()` under real concurrency. The SQLite
backend cannot express `SELECT ... FOR UPDATE SKIP LOCKED`, which is the entire
reason this backend exists, so a check that only runs single-process would miss
the exact regression it is meant to catch.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASSED: list[str] = []
SKIPPED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition and detail:
        print(f"        {detail}")


def skip(name: str) -> None:
    SKIPPED.append(name)
    print(f"  SKIP  {name}")


def main() -> int:
    dsn = (os.environ.get("IG_POSTGRES_DSN")
           or os.environ.get("IG_POSTGRES_URL"))
    if not dsn:
        print("No IG_POSTGRES_DSN set; nothing to test.")
        return 0

    try:
        from ig_postgres_store import PostgresStore
    except ImportError:
        print("psycopg2 is not installed; nothing to test.")
        return 0

    try:
        store = PostgresStore(dsn=dsn)
    except Exception as exc:
        print(f"Cannot reach Postgres ({type(exc).__name__}); nothing to test.")
        return 0

    print("== connectivity ==")
    check("connects and reports its version",
          bool(store._scalar("SELECT version()", ())))
    store.close()

    print("\n== schema ==")
    s = PostgresStore(dsn=dsn)
    cols = s._existing_columns()
    for c in ("s3_key", "image_bytes", "image_sha256", "image_format",
              "discovered_at", "updated_at", "claimed_by", "claimed_at"):
        check(f"column {c} exists", c in cols)
    check("partial claim index exists",
          "idx_posts_claim" in {r["indexname"] for r in s._execute(
              "SELECT indexname FROM pg_indexes WHERE tablename='posts'", ())})

    print("\n== API parity with the SQLite backend ==")
    s2 = PostgresStore(dsn=dsn)
    base = "PAR"
    s2._execute("DELETE FROM posts WHERE shortcode LIKE %s", (base + "%",))
    s2.record_discovered([(f"{base}000001", "sneakers"),
                          (f"{base}000002", "nike")], "seed")
    check("record_discovered returns the number of new rows",
          s2.record_discovered([(f"{base}000001", "sneakers"),
                                (f"{base}000003", "adidas")], "seed") == 1)
    check("status_of finds a discovered post",
          s2.status_of(f"{base}000001") == "discovered")
    check("status_of returns None for an unknown post",
          s2.status_of("NOPE000000") is None)
    own_pending = [r for r in s2.pending()
                   if r["shortcode"].startswith(base)]
    check("pending returns the discovered posts",
          len(own_pending) == 3, f"got {len(own_pending)} of {len(s2.pending())}")
    s2.mark_ok(f"{base}000001", query="sneakers", author="ana", caption="hola",
               image_url="http://x/1.jpg", local_image_path="out/1.jpg",
               is_video=False, source_serp_url="hashtag://sneakers",
               scraped_at="2026-01-01T00:00:00+00:00", attempts=1,
               extra={"rich_metadata": True, "likes": 10, "media_id": 42,
                      "carousel_shortcodes": ["A", "B"]})
    check("mark_ok sets status to ok", s2.status_of(f"{base}000001") == "ok")
    check("mark_ok persists likes",
          s2._scalar("SELECT likes FROM posts WHERE shortcode=%s",
                     (f"{base}000001",)) == 10)
    check("mark_ok persists media_id as BIGINT",
          s2._scalar("SELECT media_id FROM posts WHERE shortcode=%s",
                     (f"{base}000001",)) == 42)
    check("done_posts includes the finished post",
          any(r["shortcode"] == f"{base}000001" for r in s2.done_posts()))
    s2.mark_terminal(f"{base}000002", "unavailable", "soft-404")
    check("mark_terminal sets a terminal status",
          s2.status_of(f"{base}000002") == "unavailable")
    check("pending excludes terminal posts",
          f"{base}000002" not in {r["shortcode"] for r in s2.pending()})
    s2._execute("DELETE FROM errors WHERE shortcode LIKE %s", (base + "%",))
    s2.log_error(f"{base}000003", 1, "FetchError", "boom")
    check("log_error is queryable", len(s2.errors_for(f"{base}000003")) == 1,
          f"got {len(s2.errors_for(f'{base}000003'))}")
    check("counts aggregates by status",
          s2.counts().get("ok", 0) >= 1)
    check("analytics_snapshot returns totals",
          isinstance(s2.analytics_snapshot(), dict))
    s2.close()

    print("\n== claim_work: the reason this backend exists ==")
    s3 = PostgresStore(dsn=dsn)
    base = "CLM"
    s3._execute("DELETE FROM posts WHERE shortcode LIKE %s", (base + "%",))
    s3.record_discovered([(f"{base}{i:06d}", "t") for i in range(200)], "t")
    claimed = s3.claim_work(limit=5, owner="solo")
    check("claim_work reserves exactly the requested batch",
          len(claimed) == 5, f"got {len(claimed)}")
    check("claimed rows carry their owner",
          all(r["claimed_by"] == "solo" for r in claimed))
    check("claimed_at is set",
          all(r["claimed_at"] is not None for r in claimed))

    print("\n== concurrency: five scrapers must not collide ==")
    results: dict[int, list[str]] = {}
    errors: list[str] = []

    def worker(idx: int) -> None:
        try:
            st = PostgresStore(dsn=dsn)
            got = st.claim_work(limit=20, owner=f"proc-{idx}")
            results[idx] = [r["shortcode"] for r in got]
            # Hold the claim briefly, like a real scrape would.
            time.sleep(0.3)
            st.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0

    check("no scraper raised", not errors, "; ".join(errors))
    if results:
        all_codes = [c for v in results.values() for c in v]
        check("5 scrapers x 20 slots yield 100 unique posts",
              len(set(all_codes)) == 100,
              f"got {len(set(all_codes))} unique out of {len(all_codes)}")
        check("no duplicate work (SKIP LOCKED working)", len(all_codes) ==
              len(set(all_codes)))
    check("workers finished in reasonable time", elapsed < 10,
          f"took {elapsed:.1f}s")

    print("\n== reclaim_stale ==")
    stale = PostgresStore(dsn=dsn)
    stale._execute(
        "UPDATE posts SET claimed_by='dead',"
        " claimed_at=now() - make_interval(secs=>%s)"
        " WHERE shortcode LIKE %s", (86400, f"{base}%"))
    n = stale.reclaim_stale(older_than_seconds=60)
    check("reclaim_stale frees abandoned claims", n > 0, f"freed {n}")
    check("reclaimed rows are claimable again",
          len({r["shortcode"] for r in stale.claim_work(limit=100)}) > 0)
    stale._execute("DELETE FROM posts WHERE shortcode LIKE %s", (base + "%",))
    stale.close()

    s3._execute("DELETE FROM posts WHERE shortcode LIKE %s", (base + "%",))
    s3._execute("DELETE FROM posts WHERE shortcode LIKE %s", ("PAR%",))
    s3.close()

    print("\n" + "=" * 58)
    print(f"{len(PASSED)} passed, {len(SKIPPED)} skipped, {len(FAILED)} failed")
    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())