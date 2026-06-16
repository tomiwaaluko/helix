"""MinIO / S3 blob storage for large span payloads.

Span payloads (prompts, completions, tool outputs) over 32 KB are pushed to MinIO
and referenced by URI instead of being stored inline, per ``docs/data-model.md``:

    s3://helix-blobs/<yyyy>/<mm>/<dd>/<span_id>.bin

The store is a no-op surface: ``BlobStore.from_env()`` returns ``None`` when
``S3_ENDPOINT`` is unset, and ``maybe_offload`` keeps payloads inline in that case —
so local ``make eval`` never needs MinIO.

boto3 is synchronous; blocking calls run in a worker thread via ``asyncio.to_thread``.
This avoids the ``aiobotocore`` version-pin tax for a handful of calls. Addressing
style is configurable (path-style for local MinIO, virtual-host for real S3) per the
``AGENTS.md`` gotcha — never hardcode the URL shape.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

# Payloads at or under this size stay inline in the span; larger ones are offloaded.
MAX_INLINE_BYTES = 32 * 1024

_DEFAULT_BUCKET = "helix-blobs"
_BOOTSTRAP_BUCKETS = ("helix-blobs", "helix-datasets", "helix-evals")


class BlobStore:
    """Thin async wrapper over an S3-compatible object store (MinIO in dev)."""

    def __init__(self, client: Any, *, bucket: str = _DEFAULT_BUCKET) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_env(cls) -> BlobStore | None:
        """Build a ``BlobStore`` from ``S3_*`` env vars, or ``None`` when unset."""
        endpoint = os.environ.get("S3_ENDPOINT")
        if not endpoint:
            return None

        import boto3
        from botocore.config import Config

        addressing = os.environ.get("S3_ADDRESSING_STYLE", "path")
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "helix"),
            aws_secret_access_key=os.environ.get("S3_SECRET_KEY", "helixhelix"),
            region_name=os.environ.get("S3_REGION", "us-east-1"),
            config=Config(signature_version="s3v4", s3={"addressing_style": addressing}),
        )
        bucket = os.environ.get("S3_BUCKET_BLOBS", _DEFAULT_BUCKET)
        return cls(client, bucket=bucket)

    async def ensure_buckets(self, names: Iterable[str] | None = None) -> None:
        """Create the blob buckets if absent. Idempotent; call once at startup."""
        buckets = list(names) if names is not None else list(_BOOTSTRAP_BUCKETS)
        await asyncio.to_thread(self._ensure_buckets_sync, buckets)

    def _ensure_buckets_sync(self, buckets: list[str]) -> None:
        existing = {b["Name"] for b in self._client.list_buckets().get("Buckets", [])}
        for name in buckets:
            if name not in existing:
                self._client.create_bucket(Bucket=name)

    async def put(self, data: bytes, span_id: str, *, now: datetime | None = None) -> str:
        """Store *data* under the dated ``<span_id>.bin`` key; return its ``s3://`` URI.

        The content SHA-256 is written as object metadata to enable future
        opportunistic deduplication (not performed in M3).
        """
        ts = now or datetime.now(UTC)
        key = f"{ts:%Y/%m/%d}/{span_id}.bin"
        sha = hashlib.sha256(data).hexdigest()
        await asyncio.to_thread(
            lambda: self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                Metadata={"sha256": sha},
            )
        )
        return f"s3://{self._bucket}/{key}"

    def presign_get(self, uri: str, expiry: timedelta = timedelta(hours=1)) -> str:
        """Return a presigned GET URL for an ``s3://`` URI produced by ``put``."""
        bucket, key = _parse_s3_uri(uri)
        url = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=int(expiry.total_seconds()),
        )
        return str(url)

    async def put_artifact(self, data: bytes, job_id: str) -> str:
        """Store a model-checkpoint tarball under ``artifacts/<job_id>.tar.gz``.

        Unlike :meth:`put` (dated span-payload keys), embedding-job artifacts use a
        stable, job-addressable key so the ``embedding_jobs.artifact_uri`` reference
        is reproducible. Returns the ``s3://`` URI.
        """
        key = f"artifacts/{job_id}.tar.gz"
        sha = hashlib.sha256(data).hexdigest()
        await asyncio.to_thread(
            lambda: self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                Metadata={"sha256": sha},
            )
        )
        return f"s3://{self._bucket}/{key}"


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"not an s3 URI: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


async def maybe_offload(
    attrs: dict[str, Any],
    field: str,
    text: str,
    blob_store: BlobStore | None,
    span_id: str,
) -> None:
    """Record *text* on a span: inline if small, by reference if large.

    Payloads at or under :data:`MAX_INLINE_BYTES` (or any payload when *blob_store*
    is ``None``) are stored inline as ``attrs[field]``. Larger payloads are pushed to
    the blob store and referenced as ``attrs[f"{field}_uri"]`` (e.g. ``prompt_uri``,
    ``completion_uri`` — names already defined in ``docs/data-model.md``).
    """
    if blob_store is None or len(text.encode("utf-8")) <= MAX_INLINE_BYTES:
        attrs[field] = text
        return
    uri = await blob_store.put(text.encode("utf-8"), span_id)
    attrs[f"{field}_uri"] = uri
