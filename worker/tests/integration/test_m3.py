"""Integration tests for M3 infra (Redis + MinIO).

Skipped unless HELIX_INTEGRATION=1 and `make dev` is running (real Redis + MinIO).
Exercises the paths that the unit tests fake: a live exactly-once sentinel and a
blob round-trip through MinIO including a presigned GET.

    HELIX_INTEGRATION=1 \
    REDIS_URL=redis://localhost:6379/0 \
    S3_ENDPOINT=http://localhost:9100 \
    python -m pytest tests/integration/test_m3.py -v
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from helix.runtime.idempotency import Idempotency
from helix.tools.blob import BlobStore
from helix.tools.redis_conn import get_redis

pytestmark = pytest.mark.skipif(
    not os.environ.get("HELIX_INTEGRATION"),
    reason="integration test; set HELIX_INTEGRATION=1 with make dev running",
)


async def test_exactly_once_against_real_redis() -> None:
    redis = get_redis(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    assert redis is not None
    idem = Idempotency(redis, ttl_s=30)
    task_id = f"itest-{uuid.uuid4().hex}"

    assert await idem.begin(task_id, 1) is True
    assert await idem.begin(task_id, 1) is False  # duplicate skipped
    assert await idem.begin(task_id, 2) is True  # new attempt runs

    await idem.release(task_id, 1)
    assert await idem.begin(task_id, 1) is True  # released → re-runs

    await redis.aclose()


async def test_blob_round_trip_through_minio() -> None:
    store = BlobStore.from_env()
    assert store is not None, "set S3_ENDPOINT to the MinIO endpoint"
    await store.ensure_buckets()

    payload = f"hello-{uuid.uuid4().hex}".encode()
    span_id = uuid.uuid4().hex
    uri = await store.put(payload, span_id)
    assert uri.startswith("s3://helix-blobs/")
    assert uri.endswith(f"{span_id}.bin")

    url = store.presign_get(uri)
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
    resp.raise_for_status()
    assert resp.content == payload
