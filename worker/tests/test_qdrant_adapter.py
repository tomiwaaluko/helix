"""Qdrant adapter integration test against a local in-memory client.

No Docker needed: ``AsyncQdrantClient(location=":memory:")`` runs Qdrant's
local implementation in-process.
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("qdrant_client")

from qdrant_client import AsyncQdrantClient  # noqa: E402

from helix.logging import SpanLogger  # noqa: E402
from helix.tools.qdrant_adapter import Point, QdrantAdapter, ScoredPoint  # noqa: E402


async def test_create_upsert_search_via_alias(tmp_path: Path) -> None:
    client = AsyncQdrantClient(location=":memory:")
    async with QdrantAdapter(
        client=client,
        collection_alias="corpus.active",
        span_logger=SpanLogger(tmp_path / "spans.jsonl"),
    ) as adapter:
        await adapter.create_collection("corpus.base", vector_size=4, distance="Cosine")
        await adapter.set_alias("corpus.active", "corpus.base")

        points = [
            Point(id=i, vector=[float(i + 1), 1.0, 0.0, 0.0], payload={"doc_id": f"d{i}"})
            for i in range(100)
        ]
        await adapter.upsert(points)

        results = await adapter.search([1.0, 1.0, 0.0, 0.0], top_k=3)
        assert len(results) == 3
        assert all(isinstance(r, ScoredPoint) for r in results)
        assert all(r.payload["doc_id"].startswith("d") for r in results)
        assert results[0].score >= results[-1].score  # sorted by score desc

    spans = [json.loads(line) for line in (tmp_path / "spans.jsonl").read_text().splitlines()]
    by_name = {s["name"]: s for s in spans}
    assert by_name["qdrant_search"]["kind"] == "retrieval"
    assert by_name["qdrant_search"]["attributes"]["count"] == 3
    assert by_name["qdrant_search"]["attributes"]["alias"] == "corpus.active"
    assert by_name["qdrant_upsert"]["attributes"]["count"] == 100


async def test_upsert_batches(tmp_path: Path) -> None:
    client = AsyncQdrantClient(location=":memory:")
    async with QdrantAdapter(
        client=client, span_logger=SpanLogger(tmp_path / "spans.jsonl")
    ) as adapter:
        await adapter.create_collection("c", vector_size=2, distance="Dot")
        await adapter.set_alias("corpus.active", "c")
        # 250 points with batch_size 100 → 3 underlying upsert calls, all visible in search.
        await adapter.upsert(
            [Point(id=i, vector=[float(i), 1.0]) for i in range(250)], batch_size=100
        )
        results = await adapter.search([1.0, 1.0], top_k=250)
        assert len(results) == 250


def test_requires_url_or_client() -> None:
    with pytest.raises(ValueError, match="url or an injected client"):
        QdrantAdapter()
