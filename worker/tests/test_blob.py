"""Unit tests for the MinIO/S3 BlobStore and the 32 KB offload helper."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from helix.tools.blob import MAX_INLINE_BYTES, BlobStore, maybe_offload


class FakeS3:
    """In-memory stand-in for a boto3 S3 client."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}
        self.buckets: set[str] = set()

    def list_buckets(self) -> dict[str, Any]:
        return {"Buckets": [{"Name": b} for b in sorted(self.buckets)]}

    def create_bucket(self, *, Bucket: str) -> None:
        self.buckets.add(Bucket)

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, Metadata: dict[str, str] | None = None
    ) -> None:
        self.objects[(Bucket, Key)] = (Body, Metadata or {})

    def generate_presigned_url(self, op: str, *, Params: dict[str, str], ExpiresIn: int) -> str:
        return f"https://minio.local/{Params['Bucket']}/{Params['Key']}?op={op}&exp={ExpiresIn}"


def _store() -> tuple[BlobStore, FakeS3]:
    fake = FakeS3()
    return BlobStore(fake, bucket="helix-blobs"), fake


# ── put / presign ─────────────────────────────────────────────────────────────


async def test_put_uses_dated_span_key_and_returns_uri() -> None:
    store, fake = _store()
    now = datetime(2026, 6, 14, 9, 30, 0)
    uri = await store.put(b"hello", "span-abc", now=now)
    assert uri == "s3://helix-blobs/2026/06/14/span-abc.bin"
    assert ("helix-blobs", "2026/06/14/span-abc.bin") in fake.objects


async def test_put_writes_sha256_metadata() -> None:
    store, fake = _store()
    data = b"some payload"
    uri = await store.put(data, "span-1")
    _, meta = fake.objects[("helix-blobs", uri.split("helix-blobs/")[1])]
    assert meta["sha256"] == hashlib.sha256(data).hexdigest()


def test_presign_get_returns_url_for_uri() -> None:
    store, _ = _store()
    url = store.presign_get("s3://helix-blobs/2026/06/14/span-abc.bin")
    assert "helix-blobs/2026/06/14/span-abc.bin" in url


# ── ensure_buckets ────────────────────────────────────────────────────────────


async def test_ensure_buckets_is_idempotent() -> None:
    store, fake = _store()
    await store.ensure_buckets()
    assert {"helix-blobs", "helix-datasets", "helix-evals"} <= fake.buckets
    await store.ensure_buckets()  # second call must not raise
    assert {"helix-blobs", "helix-datasets", "helix-evals"} <= fake.buckets


# ── maybe_offload ─────────────────────────────────────────────────────────────


async def test_maybe_offload_inlines_small_payload() -> None:
    store, _ = _store()
    attrs: dict[str, Any] = {}
    await maybe_offload(attrs, "prompt", "short", store, "span-1")
    assert attrs == {"prompt": "short"}


async def test_maybe_offload_offloads_large_payload() -> None:
    store, _ = _store()
    attrs: dict[str, Any] = {}
    big = "x" * (MAX_INLINE_BYTES + 1)
    await maybe_offload(attrs, "prompt", big, store, "span-1")
    assert "prompt" not in attrs
    assert attrs["prompt_uri"].startswith("s3://helix-blobs/")


async def test_maybe_offload_inlines_when_store_is_none() -> None:
    attrs: dict[str, Any] = {}
    big = "x" * (MAX_INLINE_BYTES + 1)
    await maybe_offload(attrs, "completion", big, None, "span-1")
    assert attrs["completion"] == big
    assert "completion_uri" not in attrs


async def test_from_env_returns_none_without_endpoint(monkeypatch: Any) -> None:
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    assert BlobStore.from_env() is None
