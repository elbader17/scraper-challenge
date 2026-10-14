#!/usr/bin/env python3
"""
Tests for image storage (ig_objectstore).

Runs fully offline: the local and memory backends need nothing installed, and
the S3 backend is exercised only when `boto3` and `moto` are both importable
(moto gives a complete in-process S3, so the real boto3 code path is covered
without a network or credentials).

What is being pinned down here:
  - the key layout, because every analytics engine partitions on `dt=`
  - idempotency, because a resumed run must not duplicate objects
  - integrity, because a silently corrupted image is worse than a missing one
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ig_objectstore as O

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition and detail:
        print(f"        {detail}")


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 3000
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 3000


def suite(store: O.ObjectStore, label: str) -> None:
    print(f"\n== {label} ==")

    # --- key layout ---
    key = O.build_key("Dc3IVNnAQPS", "jpg",
                      datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc))
    check("key layout is images/dt=YYYY-MM-DD/{shortcode}.{ext}",
          key == "images/dt=2026-10-14/Dc3IVNnAQPS.jpg", key)
    check("partition uses the UTC date",
          O.build_key("ABC1234567", "webp",
                      datetime(2026, 1, 2, 23, 30,
                               tzinfo=timezone.utc)).endswith(
              "ABC1234567.webp") and
          "/dt=2026-01-02/" in O.build_key("ABC1234567", "webp",
                                           datetime(2026, 1, 2, 23, 30,
                                                    tzinfo=timezone.utc)))
    check("extension is normalised (uppercase/dot stripped)",
          O.build_key("ABC1234567", ".JPG",
                      datetime(2026, 10, 14, tzinfo=timezone.utc)).endswith(
              "ABC1234567.jpg"))
    try:
        O.build_key("ABC1234567", "exe",
                    datetime(2026, 10, 14, tzinfo=timezone.utc))
        _ok = False
    except ValueError:
        _ok = True
    check("unsupported extension is rejected", _ok)

    # --- put / get / exists ---
    r = store.put_image("AAAA1111111", JPEG, "jpg",
                        datetime(2026, 10, 14, tzinfo=timezone.utc))
    check("put_image returns the key, size and digest",
          r is not None and r.size == len(JPEG)
          and r.sha256 == hashlib.sha256(JPEG).hexdigest(),
          f"got {r}")
    check("object exists after put", store.exists(r.key))
    check("get returns the exact bytes", store.get(r.key) == JPEG)
    check("metadata records the content type",
          store.metadata(r.key).get("content_type") == "image/jpeg",
          str(store.metadata(r.key)))

    # --- idempotency ---
    again = store.put_image("AAAA1111111", JPEG, "jpg",
                            datetime(2026, 10, 14, tzinfo=timezone.utc))
    check("re-uploading identical bytes is skipped", again is not None
          and again.skipped, f"got {again}")

    # --- a different payload under the same key must NOT be skipped ---
    other = store.put_image("AAAA1111111", JPEG + b"X" * 64, "jpg",
                            datetime(2026, 10, 14, tzinfo=timezone.utc))
    check("different bytes under the same key are uploaded", other is not None
          and not other.skipped, f"got {other}")

    # --- tiny payloads are rejected ---
    check("a payload under 1 KB is not stored",
          store.put_image("TINY111111", b"\x00" * 10, "jpg") is None)

    # --- second object, distinct key ---
    r2 = store.put_image("BBBB2222222", WEBP, "webp",
                         datetime(2026, 10, 14, tzinfo=timezone.utc))
    check("a different extension yields a different key", r2.key != r.key)
    check("webp content type is correct",
          store.metadata(r2.key).get("content_type") == "image/webp")

    # --- delete ---
    store.delete(r2.key)
    check("delete removes the object", not store.exists(r2.key))


def main() -> int:
    # moto reads the credentials at import time, so they must exist before the
    # try/import below.
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

    root = Path(tempfile.mkdtemp(prefix="objstore-"))
    local = O.LocalObjectStore(root / "objects")

    suite(local, "LocalObjectStore (zero dependencies)")
    suite(O.MemoryObjectStore(), "MemoryObjectStore")

    # --- local specifics ---
    print("\n== LocalObjectStore specifics ==")
    sidecars = list((root / "objects").rglob("*.meta"))
    check("a .meta sidecar is written next to each object",
          len(sidecars) >= 1, f"{len(sidecars)} found")
    check("url() is a file:// URI", local.url(
        "images/dt=2026-10-14/AAAA1111111.jpg").startswith("file://"))
    try:
        local.get("../escape.jpg")
        _safe = False
    except Exception:
        _safe = True
    check("keys that escape the root are refused", _safe)
    st = local.stats()
    check("stats report the backend and a byte count",
          st["backend"] == "local" and st["bytes"] > 0, str(st))

    # --- factory selection ---
    print("\n== factory selection ==")
    saved = {k: os.environ.pop(k, None)
             for k in ("IG_S3_BUCKET", "IG_OBJECT_STORE_ROOT", "IG_POSTGRES_DSN")}
    try:
        check("no configuration -> None (behaviour unchanged)",
              O.make_object_store() is None)
        os.environ["IG_OBJECT_STORE_ROOT"] = str(root / "env-objects")
        check("IG_OBJECT_STORE_ROOT selects the local backend",
              type(O.make_object_store()).__name__ == "LocalObjectStore")
        try:
            import boto3  # noqa: F401
            _has_boto = True
        except ImportError:
            _has_boto = False
        os.environ["IG_S3_BUCKET"] = "ig-posts"
        if _has_boto:
            check("IG_S3_BUCKET selects the S3 backend",
                  type(O.make_object_store()).__name__ == "S3ObjectStore")
        else:
            print("  SKIP  S3 selection (boto3 not installed)")
        check("an explicit backend wins over the environment",
              type(O.make_object_store(backend="memory")).__name__
              == "MemoryObjectStore")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    # --- S3 backend against an in-process S3 (no network, no credentials) ---
    print("\n== S3ObjectStore ==")
    try:
        import boto3  # noqa: F401
        from moto import mock_aws
    except ImportError:
        print("  SKIP  boto3/moto not installed; S3 path not exercised here")
        print("        (install with: pip install boto3 moto)")
    else:
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
        os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
        with mock_aws():
            s3 = O.S3ObjectStore(bucket="ig-posts", region="us-east-1")
            suite(s3, "S3ObjectStore (against moto)")

            print("\n== S3 compatibility ==")
            client = boto3.client("s3", region_name="us-east-1")
            keys = [o["Key"] for o in
                    client.list_objects_v2(Bucket="ig-posts").get("Contents", [])]
            check("objects are readable by a plain boto3 client", len(keys) >= 1,
                  f"{len(keys)} objects")
            check("keys are S3-native (forward slashes, no leading slash)",
                  all(not k.startswith("/") and "\\" not in k for k in keys),
                  str(keys))
            check("url() points at the bucket",
                  "ig-posts" in s3.url(keys[0]))

            print("\n== migration helper ==")
            src = root / "legacy-run"
            src.mkdir(parents=True, exist_ok=True)
            # Use shortcodes that the shared suite above did not already touch,
            # so the expected counts are exact.
            for name, payload, ext in (("CCC3333333", JPEG, "jpg"),
                                       ("DDD4444444", WEBP, "webp"),
                                       ("EEE5555555", JPEG, "webp")):
                (src / f"{name}.{ext}").write_bytes(payload)
            n = len(list(src.iterdir()))
            first = O.migrate_local(src, s3)
            check("migration uploads every image", first["uploaded"] == n,
                  str(first))
            second = O.migrate_local(src, s3)
            check("re-running the migration uploads nothing",
                  second["uploaded"] == 0 and second["skipped"] == n,
                  str(second))

    print("\n" + "=" * 58)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())