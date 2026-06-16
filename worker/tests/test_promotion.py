"""Unit tests for the promotion + canary-eval module.

All heavy operations (corpus indexing, Qdrant retrieval, model loading) are
stubbed so the tests run without Docker, the 0.5 GB model, or gradient descent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from helix.eval.harness import Example
from helix.rag.promotion.promote import (
    PromoteResult,
    _bootstrap_ci,
    candidate_collection,
    promote_candidate,
)
from helix.rag.trainer.train import DEFAULT_BASE_MODEL, TrainConfig
from helix.runtime.sqlite_store import SqliteStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GOLD_ID = "doc_gold"
_NEG_ID = "doc_neg"

_EXAMPLE = Example(
    id="ex_001",
    input={"question": "What is the capital of France?"},
    expected_output={
        "answer": "Paris",
        "supporting_facts": [{"doc_id": _GOLD_ID, "sent_id": 0}],
    },
)

_EXAMPLE_NO_GOLD = Example(
    id="ex_002",
    input={"question": "Unknown?"},
    expected_output={"answer": "?", "supporting_facts": []},
)


class _StubAdapter:
    """Minimal QdrantAdapter stand-in that records alias swaps."""

    def __init__(self) -> None:
        self.aliases: dict[str, str] = {}

    async def set_alias(self, alias: str, collection: str) -> None:
        self.aliases[alias] = collection


def _make_index_fn(
    recorded: list[tuple[str, str]] | None = None,
) -> Any:
    async def _index(corpus_path: str, collection: str) -> None:
        if recorded is not None:
            recorded.append((corpus_path, collection))

    return _index


def _make_retrieve_fn(
    baseline_ids: list[str],
    candidate_ids: list[str],
    candidate_coll: str,
) -> Any:
    async def _retrieve(query: str, collection: str, k: int) -> list[str]:
        if collection == candidate_coll:
            return candidate_ids[:k]
        return baseline_ids[:k]

    return _retrieve


async def _make_store(tmp_path: Path, job_id: str = "job-1") -> SqliteStore:
    store = SqliteStore(tmp_path / "helix.db")
    await store.__aenter__()
    await store.create_embedding_job(DEFAULT_BASE_MODEL, TrainConfig().to_dict(), job_id=job_id)
    return store


# ---------------------------------------------------------------------------
# candidate_collection name
# ---------------------------------------------------------------------------


def test_candidate_collection_name() -> None:
    assert candidate_collection("abc-123") == "corpus.candidate.abc-123"


# ---------------------------------------------------------------------------
# _bootstrap_ci edge cases
# ---------------------------------------------------------------------------


def test_bootstrap_ci_empty() -> None:
    ci = _bootstrap_ci([], samples=100, seed=0)
    assert ci["mean"] == 0.0
    assert ci["n"] == 0


def test_bootstrap_ci_single_value() -> None:
    ci = _bootstrap_ci([0.75], samples=100, seed=0)
    assert ci["mean"] == pytest.approx(0.75)
    assert ci["ci_low"] == pytest.approx(0.75)
    assert ci["ci_high"] == pytest.approx(0.75)


def test_bootstrap_ci_multiple_values() -> None:
    values = [0.5, 0.6, 0.7, 0.8, 0.9]
    ci = _bootstrap_ci(values, samples=1000, seed=42)
    assert ci["ci_low"] <= ci["mean"] <= ci["ci_high"]
    assert ci["n"] == 5


# ---------------------------------------------------------------------------
# promote_candidate
# ---------------------------------------------------------------------------


async def test_promote_candidate_rejects_empty_examples(tmp_path: Path) -> None:
    store = await _make_store(tmp_path)
    try:
        with pytest.raises(ValueError, match="at least one example"):
            await promote_candidate(
                "ckpt",
                "corpus.jsonl",
                [],
                "job-1",
                adapter=_StubAdapter(),  # type: ignore[arg-type]
                store=store,
            )
    finally:
        await store.__aexit__(None, None, None)


async def test_promote_candidate_promoted_when_candidate_better(
    tmp_path: Path,
) -> None:
    coll = candidate_collection("job-1")
    adapter = _StubAdapter()
    store = await _make_store(tmp_path)
    try:
        result = await promote_candidate(
            "ckpt",
            "corpus.jsonl",
            [_EXAMPLE],
            "job-1",
            adapter=adapter,  # type: ignore[arg-type]
            store=store,
            index_fn=_make_index_fn(),
            retrieve_fn=_make_retrieve_fn(
                baseline_ids=[_NEG_ID],  # baseline misses gold
                candidate_ids=[_GOLD_ID],  # candidate finds gold
                candidate_coll=coll,
            ),
        )
    finally:
        await store.__aexit__(None, None, None)

    assert result.status == "promoted"
    assert result.collection == coll
    assert adapter.aliases.get("corpus.active") == coll


async def test_promote_candidate_archived_when_candidate_worse(
    tmp_path: Path,
) -> None:
    coll = candidate_collection("job-1")
    adapter = _StubAdapter()
    store = await _make_store(tmp_path)
    try:
        result = await promote_candidate(
            "ckpt",
            "corpus.jsonl",
            [_EXAMPLE],
            "job-1",
            adapter=adapter,  # type: ignore[arg-type]
            store=store,
            index_fn=_make_index_fn(),
            retrieve_fn=_make_retrieve_fn(
                baseline_ids=[_GOLD_ID],  # baseline finds gold
                candidate_ids=[_NEG_ID],  # candidate misses gold
                candidate_coll=coll,
            ),
        )
    finally:
        await store.__aexit__(None, None, None)

    assert result.status == "archived"
    assert "corpus.active" not in adapter.aliases


async def test_promote_candidate_archived_when_tied(tmp_path: Path) -> None:
    """Equal recall → archive (not promoted) — no alias swap without strict lift."""
    coll = candidate_collection("job-1")
    adapter = _StubAdapter()
    store = await _make_store(tmp_path)
    try:
        result = await promote_candidate(
            "ckpt",
            "corpus.jsonl",
            [_EXAMPLE],
            "job-1",
            adapter=adapter,  # type: ignore[arg-type]
            store=store,
            index_fn=_make_index_fn(),
            retrieve_fn=_make_retrieve_fn(
                baseline_ids=[_GOLD_ID],
                candidate_ids=[_GOLD_ID],
                candidate_coll=coll,
            ),
        )
    finally:
        await store.__aexit__(None, None, None)

    assert result.status == "archived"


async def test_promote_candidate_calls_index_fn(tmp_path: Path) -> None:
    recorded: list[tuple[str, str]] = []
    coll = candidate_collection("job-1")
    store = await _make_store(tmp_path)
    try:
        await promote_candidate(
            "ckpt",
            "my_corpus.jsonl",
            [_EXAMPLE],
            "job-1",
            adapter=_StubAdapter(),  # type: ignore[arg-type]
            store=store,
            index_fn=_make_index_fn(recorded),
            retrieve_fn=_make_retrieve_fn([], [], coll),
        )
    finally:
        await store.__aexit__(None, None, None)

    assert recorded == [("my_corpus.jsonl", coll)]


async def test_promote_candidate_updates_job_on_promotion(tmp_path: Path) -> None:
    coll = candidate_collection("job-1")
    store = await _make_store(tmp_path)
    try:
        await promote_candidate(
            "ckpt",
            "corpus.jsonl",
            [_EXAMPLE],
            "job-1",
            adapter=_StubAdapter(),  # type: ignore[arg-type]
            store=store,
            index_fn=_make_index_fn(),
            retrieve_fn=_make_retrieve_fn([_NEG_ID], [_GOLD_ID], coll),
        )
        job = await store.get_embedding_job("job-1")
    finally:
        await store.__aexit__(None, None, None)

    assert job is not None
    assert job.status == "promoted"
    assert job.artifact_uri == "data/models/job-1"
    assert job.promoted_at is not None
    assert job.metrics is not None
    assert "before" in job.metrics
    assert "after" in job.metrics
    assert "delta_mean" in job.metrics


async def test_promote_candidate_updates_job_on_archive(tmp_path: Path) -> None:
    coll = candidate_collection("job-1")
    store = await _make_store(tmp_path)
    try:
        await promote_candidate(
            "ckpt",
            "corpus.jsonl",
            [_EXAMPLE],
            "job-1",
            adapter=_StubAdapter(),  # type: ignore[arg-type]
            store=store,
            index_fn=_make_index_fn(),
            retrieve_fn=_make_retrieve_fn([_GOLD_ID], [_NEG_ID], coll),
        )
        job = await store.get_embedding_job("job-1")
    finally:
        await store.__aexit__(None, None, None)

    assert job is not None
    assert job.status == "archived"
    assert job.artifact_uri is None
    assert job.promoted_at is None


async def test_promote_candidate_metrics_structure(tmp_path: Path) -> None:
    coll = candidate_collection("job-1")
    store = await _make_store(tmp_path)
    try:
        result = await promote_candidate(
            "ckpt",
            "corpus.jsonl",
            [_EXAMPLE],
            "job-1",
            adapter=_StubAdapter(),  # type: ignore[arg-type]
            store=store,
            index_fn=_make_index_fn(),
            retrieve_fn=_make_retrieve_fn([_GOLD_ID], [_GOLD_ID], coll),
        )
    finally:
        await store.__aexit__(None, None, None)

    assert set(result.metrics.keys()) == {"before", "after", "delta_mean"}
    for key in ("before", "after"):
        ci = result.metrics[key]
        assert "mean" in ci
        assert "ci_low" in ci
        assert "ci_high" in ci
        assert "n" in ci


async def test_promote_candidate_sets_evaluating_before_indexing(
    tmp_path: Path,
) -> None:
    status_during_index: list[str] = []
    coll = candidate_collection("job-1")
    store = await _make_store(tmp_path)

    async def _instrumented_index(corpus_path: str, collection: str) -> None:
        job = await store.get_embedding_job("job-1")
        if job is not None:
            status_during_index.append(job.status)

    try:
        await promote_candidate(
            "ckpt",
            "corpus.jsonl",
            [_EXAMPLE],
            "job-1",
            adapter=_StubAdapter(),  # type: ignore[arg-type]
            store=store,
            index_fn=_instrumented_index,
            retrieve_fn=_make_retrieve_fn([_GOLD_ID], [_GOLD_ID], coll),
        )
    finally:
        await store.__aexit__(None, None, None)

    assert status_during_index == ["evaluating"]


async def test_promote_candidate_example_with_no_gold_gives_zero_recall(
    tmp_path: Path,
) -> None:
    """Examples with empty supporting_facts always score 0 — CI should be 0/0."""
    coll = candidate_collection("job-1")
    store = await _make_store(tmp_path)
    try:
        result = await promote_candidate(
            "ckpt",
            "corpus.jsonl",
            [_EXAMPLE_NO_GOLD],
            "job-1",
            adapter=_StubAdapter(),  # type: ignore[arg-type]
            store=store,
            index_fn=_make_index_fn(),
            retrieve_fn=_make_retrieve_fn([_GOLD_ID], [_GOLD_ID], coll),
        )
    finally:
        await store.__aexit__(None, None, None)

    assert result.metrics["before"]["mean"] == pytest.approx(0.0)
    assert result.metrics["after"]["mean"] == pytest.approx(0.0)
    assert result.status == "archived"


async def test_promote_candidate_returns_correct_result_type(
    tmp_path: Path,
) -> None:
    coll = candidate_collection("job-2")
    store = await _make_store(tmp_path, job_id="job-2")
    try:
        result = await promote_candidate(
            "ckpt",
            "corpus.jsonl",
            [_EXAMPLE],
            "job-2",
            adapter=_StubAdapter(),  # type: ignore[arg-type]
            store=store,
            index_fn=_make_index_fn(),
            retrieve_fn=_make_retrieve_fn([_GOLD_ID], [_GOLD_ID], coll),
        )
    finally:
        await store.__aexit__(None, None, None)

    assert isinstance(result, PromoteResult)
    assert result.job_id == "job-2"
    assert result.collection == coll
