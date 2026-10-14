#!/usr/bin/env python3
"""
Image storage, with a local emulation that costs nothing.

WHY AN ABSTRACTION
------------------
The plan calls for S3. But the whole point of this module is that S3 is *not*
required: at the volumes this project runs at, the images are ~17 GB/month, and
the interesting question is not "which cloud" but "where do the bytes go and how
do we prove they arrived intact".

Three backends, one interface:

    S3ObjectStore    AWS S3, or anything S3-compatible: MinIO, Ceph, Cloudflare
                     R2, Backblaze B2. Selected by IG_S3_BUCKET. Needs boto3.
    LocalObjectStore Plain filesystem, zero dependencies, same key layout.
                     Selected by IG_OBJECT_STORE_ROOT. This is the default when
                     no bucket is configured.
    MemoryObjectStore In-process, for tests.

All three speak the same key layout, so a dataset produced locally can be moved
to S3 without rewriting anything:

    images/dt=2026-10-14/Dc3IVNnAQPS.jpg

Why partition by `dt` = scrape date and not publication date: the publication
date is *inferred* (decoded from the shortcode snowflake) and may be missing or
wrong. The scrape date is always known. Analytics can re-partition later; the
write has to be deterministic.

Idempotency: the key is derived from the shortcode alone, so re-running a post
overwrites instead of duplicating. `put_image()` also compares the SHA-256
against what is already stored and skips a byte-identical re-upload, which
matters when a run is resumed.

ENVIRONMENT
    IG_OBJECT_STORE_ROOT   enables LocalObjectStore at that directory
    IG_S3_BUCKET          enables S3ObjectStore (boto3)
    IG_S3_ENDPOINT        for S3-compatible services (MinIO/Ceph/R2)
    IG_S3_REGION          defaults to us-east-1
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_ENDPOINT_URL
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional

KEY_PREFIX = "images"
CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "png": "image/png",
    "gif": "image/gif",
}


def build_key(shortcode: str, ext: str, when: Optional[datetime] = None) -> str:
    """Object key for a post image.

    `dt=` is the Hive-style partition prefix, which is what every analytics
    engine understands natively (Athena, Snowflake, BigQuery, DuckDB).
    """
    when = when or datetime.now(timezone.utc)
    ext = (ext or "").lower().lstrip(".")
    if ext not in CONTENT_TYPES:
        raise ValueError(f"unsupported image extension: {ext!r}")
    return (f"{KEY_PREFIX}/dt={when.astimezone(timezone.utc).date().isoformat()}"
            f"/{shortcode}.{ext}")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    sha256: str
    content_type: str
    skipped: bool = False   # True when an identical object was already there


class ObjectStore:
    """Common interface. `name` shows up in the logs."""

    name = "object-store"

    def put(self, key: str, data: bytes, content_type: str) -> StoredObject:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def url(self, key: str) -> str:
        raise NotImplementedError

    # -- shared ----------------------------------------------------------
    def put_image(self, shortcode: str, data: bytes, ext: str,
                  when: Optional[datetime] = None) -> Optional[StoredObject]:
        """Stores an image, skipping the upload if the bytes are already there.

        Returns None when the payload is too small to be a real image, mirroring
        `download_image`'s own guard, so a truncated body never reaches storage.
        """
        if len(data) < 1024:
            return None
        key = build_key(shortcode, ext, when)
        digest = sha256_of(data)
        if self.exists(key) and self._matches(key, digest):
            return StoredObject(key, len(data), digest,
                                CONTENT_TYPES[ext.lower().lstrip(".")], True)
        return self.put(key, data, CONTENT_TYPES[ext.lower().lstrip(".")])

    def _matches(self, key: str, digest: str) -> bool:
        """True when the stored object already has this exact content."""
        try:
            stored = self.metadata(key)
        except Exception:
            return False
        return stored.get("sha256") == digest

    def metadata(self, key: str) -> dict[str, Any]:
        return {}

    def stats(self) -> dict[str, Any]:
        return {"backend": self.name}


class LocalObjectStore(ObjectStore):
    """Filesystem-backed store. Zero dependencies, zero cost.

    Mirrors the S3 key layout as real directories, so a local run produces a tree
    that can be uploaded verbatim with `aws s3 sync` or `mc mirror`.
    """

    name = "local"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys are generated internally, but refuse anything that escapes root.
        p = (self.root / PurePosixPath(key)).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError(f"key escapes root: {key!r}")
        return p

    def put(self, key: str, data: bytes, content_type: str) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        # Sidecar keeps Content-Type and SHA-256, which a plain filesystem does
        # not otherwise record, so `metadata()` works the same on both backends.
        path.with_suffix(path.suffix + ".meta").write_text(
            f"{content_type}\n{sha256_of(data)}\n{len(data)}\n", encoding="utf-8")
        return StoredObject(key, len(data), sha256_of(data), content_type)

    def metadata(self, key: str) -> dict[str, Any]:
        side = self._path(key).with_suffix(self._path(key).suffix + ".meta")
        if not side.exists():
            return {}
        parts = side.read_text(encoding="utf-8").split("\n")
        return {"content_type": parts[0] if parts else "",
                "sha256": parts[1] if len(parts) > 1 else "",
                "size": int(parts[2]) if len(parts) > 2 and parts[2] else 0}

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()
        side = path.with_suffix(path.suffix + ".meta")
        if side.exists():
            side.unlink()

    def url(self, key: str) -> str:
        return self._path(key).as_uri()

    def stats(self) -> dict[str, Any]:
        files = [p for p in self.root.rglob("*")
                 if p.is_file() and p.suffix not in (".meta",)]
        return {"backend": self.name, "root": str(self.root),
                "objects": len(files),
                "bytes": sum(p.stat().st_size for p in files)}


class MemoryObjectStore(ObjectStore):
    """In-process store, for tests."""

    name = "memory"

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, data: bytes, content_type: str) -> StoredObject:
        self.objects[key] = (data, content_type)
        return StoredObject(key, len(data), sha256_of(data), content_type)

    def metadata(self, key: str) -> dict[str, Any]:
        if key not in self.objects:
            return {}
        data, ct = self.objects[key]
        return {"content_type": ct, "sha256": sha256_of(data), "size": len(data)}

    def exists(self, key: str) -> bool:
        return key in self.objects

    def get(self, key: str) -> bytes:
        return self.objects[key][0]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def url(self, key: str) -> str:
        return f"memory://{key}"


class S3ObjectStore(ObjectStore):
    """AWS S3 or any S3-compatible service (MinIO, Ceph, R2, B2).

    `boto3` is imported lazily so the rest of the project keeps working without
    it — which matters because the test suite and the default local mode must
    not depend on a cloud SDK being installed.
    """

    name = "s3"

    def __init__(self, bucket: Optional[str] = None, endpoint: Optional[str] = None,
                 region: Optional[str] = None) -> None:
        try:
            import boto3  # noqa: F401
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "boto3 is required for the S3 backend:\n"
                "  pip install boto3\n"
                "Or use the local backend instead:\n"
                "  export IG_OBJECT_STORE_ROOT=./data/objects") from exc

        self.bucket = bucket or os.environ.get("IG_S3_BUCKET")
        if not self.bucket:
            raise ValueError("IG_S3_BUCKET is required for the S3 backend")
        self.endpoint = (endpoint or os.environ.get("IG_S3_ENDPOINT")
                         or os.environ.get("AWS_ENDPOINT_URL"))
        self.region = region or os.environ.get("IG_S3_REGION", "us-east-1")

        self._client = boto3.client(
            "s3", endpoint_url=self.endpoint, region_name=self.region,
            config=Config(signature_version="s3v4",
                          retries={"max_attempts": 3, "mode": "standard"}))
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """Creates the bucket if missing.

        On real AWS this only works in some regions and usually the bucket is
        provisioned separately; on MinIO it just works. A failure here is not
        fatal: the caller may point at a pre-existing bucket.
        """
        from botocore.exceptions import ClientError
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError:
            try:
                kwargs: dict[str, Any] = {"Bucket": self.bucket}
                if self.region and self.region != "us-east-1":
                    kwargs["CreateBucketConfiguration"] = {
                        "LocationConstraint": self.region}
                self._client.create_bucket(**kwargs)
            except ClientError as exc:
                LOG.warning("bucket %s not usable yet: %s", self.bucket, exc)

    def put(self, key: str, data: bytes, content_type: str) -> StoredObject:
        digest = sha256_of(data)
        # The checksum header lets S3 verify integrity server-side and surfaces a
        # corrupted transfer as an error instead of storing silent garbage.
        self._client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
            Metadata={"sha256": digest})
        return StoredObject(key, len(data), digest, content_type)

    def metadata(self, key: str) -> dict[str, Any]:
        head = self._client.head_object(Bucket=self.bucket, Key=key)
        return {"content_type": head.get("ContentType", ""),
                "sha256": (head.get("Metadata") or {}).get("sha256", ""),
                "size": head.get("ContentLength", 0)}

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def get(self, key: str) -> bytes:
        return self._client.get_object(
            Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def url(self, key: str) -> str:
        if self.endpoint:
            return f"{self.endpoint.rstrip('/')}/{self.bucket}/{key}"
        return f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{key}"

    def stats(self) -> dict[str, Any]:
        return {"backend": self.name, "bucket": self.bucket,
                "endpoint": self.endpoint or "aws"}


LOG = logging.getLogger("objects")


def make_object_store(backend: Optional[str] = None,
                      root: Optional[str] = None,
                      bucket: Optional[str] = None,
                      endpoint: Optional[str] = None,
                      **kwargs: Any) -> Optional[ObjectStore]:
    """Returns the configured store, or None when no object storage is wanted.

    Selection order:
      1. explicit backend
      2. IG_S3_BUCKET -> S3 (or any S3-compatible endpoint)
      3. IG_OBJECT_STORE_ROOT -> local filesystem
      4. None: keep images on disk only

    Returning None rather than defaulting to a store is deliberate: without
    configuration the scraper behaves exactly as it did before object storage
    existed.
    """
    if backend is None:
        if bucket or os.environ.get("IG_S3_BUCKET"):
            backend = "s3"
        elif root or os.environ.get("IG_OBJECT_STORE_ROOT"):
            backend = "local"
        else:
            return None

    if backend == "s3":
        return S3ObjectStore(bucket=bucket, endpoint=endpoint, **kwargs)
    if backend == "local":
        return LocalObjectStore(root or os.environ.get("IG_OBJECT_STORE_ROOT",
                                                       "./data/objects"))
    if backend == "memory":
        return MemoryObjectStore()
    if backend in (None, "none", ""):
        return None
    raise ValueError(f"unknown object store backend: {backend!r}")


def migrate_local(src_dir: str | Path, store: ObjectStore) -> dict[str, Any]:
    """Uploads an existing on-disk run into an object store.

    Idempotent: identical bytes are skipped, so it is safe to re-run after a
    partial failure.
    """
    src = Path(src_dir)
    moved = skipped = failed = 0
    for path in sorted(src.glob("*.jpg")) + sorted(src.glob("*.webp")):
        data = path.read_bytes()
        try:
            result = store.put_image(path.stem, data, path.suffix.lstrip("."))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            LOG.warning("upload failed for %s: %s", path.name, exc)
            continue
        if result is None:
            failed += 1
            continue
        if result.skipped:
            skipped += 1
        else:
            moved += 1
    return {"uploaded": moved, "skipped": skipped, "failed": failed,
            "backend": store.name}


import logging  # noqa: E402  (kept last so LOG above resolves)