"""Unit tests for the handle_finetune_job worker handler.

Stubs out mine_from_clickhouse, train_embedding, promote_candidate, and the
SQLiteStore so the tests run without ClickHouse, Qdrant, or a GPU.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helix.logging import SpanLogger
from helix.rag.miner.miner import FailureCase
from helix.rag.promotion.promote import PromoteResult
from helix.rag.trainer.train import TrainConfig, TrainResult
from helix.worker.__main__ import _run_finetune_job

_CASE = FailureCase(
    example_id="ex-001",
    span_id="sp-001",
    query="Who was the first president?",
    gold_doc_id="doc-wash",
    gold_text="George Washington was the first president.",
    retrieved_top_k=[{"doc_id": "doc-lincoln", "score": 0.8, "rank": 1}],
    hard_negatives=["doc-lincoln"],
    signature="exact_match_miss",
)

_TRIPLET = MagicMock()
_TRAIN_RESULT = TrainResult(
    base_model="nomic-ai/nomic-embed-text-v1.5",
    output_dir="/tmp/finetune-test-job",
    triplets_count=3,
    config=TrainConfig(),
)
_PROMOTE_RESULT = PromoteResult(
    job_id="test-job",
    status="promoted",
    collection="corpus.candidate.test-job",
    metrics={"before": {"mean": 0.72}, "after": {"mean": 0.81}},
)


@pytest.fixture()
def span_logger() -> SpanLogger:
    return SpanLogger()


@pytest.mark.asyncio
async def test_run_finetune_job_promoted(tmp_path: Any, span_logger: SpanLogger) -> None:
    """Full mine→train→promote path returns the right output dict when promoted."""
    with (
        patch("helix.worker.__main__.mine_from_clickhouse", new_callable=AsyncMock) as mock_mine,
        patch("helix.worker.__main__.build_triplets", return_value=[_TRIPLET, _TRIPLET, _TRIPLET]),
        patch("helix.worker.__main__.train_embedding", return_value=_TRAIN_RESULT),
        patch("helix.worker.__main__.promote_candidate", new_callable=AsyncMock) as mock_promote,
        patch("helix.worker.__main__.SqliteStore") as MockStore,
        patch("helix.worker.__main__.QdrantAdapter"),
        patch("helix.worker.__main__.load_dataset", return_value=[MagicMock()]),
        patch.dict(
            "os.environ",
            {"HELIX_CORPUS_PATH": "data/corpus.jsonl", "QDRANT_URL": "http://localhost:6333"},
        ),
    ):
        mock_mine.return_value = [_CASE]
        mock_promote.return_value = _PROMOTE_RESULT

        store_instance = AsyncMock()
        store_instance.__aenter__ = AsyncMock(return_value=store_instance)
        store_instance.__aexit__ = AsyncMock(return_value=False)
        store_instance.create_embedding_job = AsyncMock(return_value=MagicMock(id="embed-job-id"))
        MockStore.return_value = store_instance

        result = await _run_finetune_job(
            job_id="test-job",
            train_split="evals/datasets/hotpotqa_train_1000.jsonl",
            eval_split="evals/datasets/hotpotqa_dev_100.jsonl",
            ch_http_url="http://localhost:8123",
            models_dir=str(tmp_path),
            span_logger=span_logger,
        )

    assert result["job_id"] == "test-job"
    assert result["outcome"] == "promoted"
    assert result["failures"] == 1
    assert result["triplets"] == 3
    assert result["before_recall"] == pytest.approx(0.72)
    assert result["after_recall"] == pytest.approx(0.81)
    # No S3 configured (BlobStore.from_env returns None) → no artifact_uri key.
    assert "artifact_uri" not in result


@pytest.mark.asyncio
async def test_run_finetune_job_uploads_artifact(tmp_path: Any, span_logger: SpanLogger) -> None:
    """When S3 is configured, the checkpoint is tarred + uploaded and the URI returned."""
    output_dir = tmp_path / "art-job"
    output_dir.mkdir()
    (output_dir / "model.bin").write_bytes(b"weights")

    blob = MagicMock()
    blob.put_artifact = AsyncMock(return_value="s3://helix-blobs/artifacts/art-job.tar.gz")

    with (
        patch("helix.worker.__main__.mine_from_clickhouse", new_callable=AsyncMock) as mock_mine,
        patch("helix.worker.__main__.build_triplets", return_value=[_TRIPLET]),
        patch("helix.worker.__main__.train_embedding", return_value=_TRAIN_RESULT),
        patch("helix.worker.__main__.promote_candidate", new_callable=AsyncMock) as mock_promote,
        patch("helix.worker.__main__.SqliteStore") as MockStore,
        patch("helix.worker.__main__.QdrantAdapter"),
        patch("helix.worker.__main__.load_dataset", return_value=[MagicMock()]),
        patch("helix.worker.__main__.BlobStore.from_env", return_value=blob),
        patch.dict("os.environ", {"S3_ENDPOINT": "http://localhost:9100"}),
    ):
        mock_mine.return_value = [_CASE]
        mock_promote.return_value = _PROMOTE_RESULT
        store_instance = AsyncMock()
        store_instance.__aenter__ = AsyncMock(return_value=store_instance)
        store_instance.__aexit__ = AsyncMock(return_value=False)
        store_instance.create_embedding_job = AsyncMock(return_value=MagicMock(id="e"))
        MockStore.return_value = store_instance

        result = await _run_finetune_job(
            job_id="art-job",
            train_split="t.jsonl",
            eval_split="e.jsonl",
            ch_http_url="http://localhost:8123",
            models_dir=str(tmp_path),
            span_logger=span_logger,
        )

    assert result["artifact_uri"] == "s3://helix-blobs/artifacts/art-job.tar.gz"
    blob.put_artifact.assert_awaited_once()
    # The uploaded payload is a non-empty gzip tarball.
    uploaded_bytes = blob.put_artifact.await_args.args[0]
    assert isinstance(uploaded_bytes, bytes) and len(uploaded_bytes) > 0


@pytest.mark.asyncio
async def test_run_finetune_job_no_failures(span_logger: SpanLogger) -> None:
    """Returns no_failures when mine_from_clickhouse returns empty list."""
    with patch("helix.worker.__main__.mine_from_clickhouse", new_callable=AsyncMock) as mock_mine:
        mock_mine.return_value = []
        result = await _run_finetune_job(
            job_id="job-nf",
            train_split="train.jsonl",
            eval_split="eval.jsonl",
            ch_http_url="http://localhost:8123",
            span_logger=span_logger,
        )

    assert result["outcome"] == "no_failures"
    assert result["failures"] == 0
    assert result["triplets"] == 0


@pytest.mark.asyncio
async def test_run_finetune_job_no_clickhouse_url(span_logger: SpanLogger) -> None:
    """Raises RuntimeError when CLICKHOUSE_HTTP_URL is not set."""
    with pytest.raises(RuntimeError, match="CLICKHOUSE_HTTP_URL"):
        await _run_finetune_job(
            job_id="job-nc",
            train_split="train.jsonl",
            eval_split="eval.jsonl",
            ch_http_url=None,
            span_logger=span_logger,
        )
