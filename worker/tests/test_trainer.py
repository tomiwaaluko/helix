"""Unit tests for the embedding trainer: triplet construction, train loop, jobs.

The training backend is stubbed, so these run without the 0.5 GB model or any
gradient descent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helix.rag.miner.miner import FailureCase
from helix.rag.trainer.train import (
    DEFAULT_BASE_MODEL,
    TrainConfig,
    Triplet,
    train_embedding,
)
from helix.rag.trainer.triplets import build_triplets
from helix.runtime.sqlite_store import SqliteStore
from helix.tools.embedder import DOCUMENT_PREFIX, QUERY_PREFIX

_GOLD = "wiki_gold"
_NEG_A = "wiki_neg_a"
_NEG_B = "wiki_neg_b"

_CORPUS = {
    _GOLD: "The gold passage with the answer.",
    _NEG_A: "A distractor passage that was wrongly retrieved.",
    _NEG_B: "Another distractor passage.",
}


def _case(
    *,
    gold_text: str = _CORPUS[_GOLD],
    hard_negatives: list[str] | None = None,
    query: str = "what is the answer?",
) -> FailureCase:
    return FailureCase(
        example_id="ex_001",
        span_id="span_1",
        query=query,
        gold_doc_id=_GOLD,
        gold_text=gold_text,
        retrieved_top_k=[],
        hard_negatives=hard_negatives if hard_negatives is not None else [_NEG_A, _NEG_B],
        signature="semantic_mismatch",
    )


# ---------------------------------------------------------------------------
# build_triplets
# ---------------------------------------------------------------------------


def test_build_triplets_applies_prefixes() -> None:
    (triplet,) = build_triplets([_case()], _CORPUS)
    assert triplet.query == QUERY_PREFIX + "what is the answer?"
    assert triplet.positive == DOCUMENT_PREFIX + _CORPUS[_GOLD]
    assert triplet.negatives[0] == DOCUMENT_PREFIX + _CORPUS[_NEG_A]


def test_build_triplets_caps_negatives() -> None:
    (triplet,) = build_triplets([_case()], _CORPUS, negatives_per_query=1)
    assert len(triplet.negatives) == 1


def test_build_triplets_skips_case_without_gold_text() -> None:
    assert build_triplets([_case(gold_text="")], _CORPUS) == []


def test_build_triplets_skips_case_without_resolvable_negative() -> None:
    # Hard negatives not in the corpus → no usable negative → dropped by default.
    assert build_triplets([_case(hard_negatives=["wiki_missing"])], _CORPUS) == []


def test_build_triplets_allows_no_negative_when_not_required() -> None:
    (triplet,) = build_triplets([_case(hard_negatives=[])], _CORPUS, require_negative=False)
    assert triplet.negatives == ()


def test_triplet_to_texts_order() -> None:
    triplet = Triplet(query="q", positive="p", negatives=("n1", "n2"))
    assert triplet.to_texts() == ["q", "p", "n1", "n2"]


# ---------------------------------------------------------------------------
# train_embedding (stubbed backend)
# ---------------------------------------------------------------------------


def test_train_embedding_invokes_backend_and_returns_result(tmp_path: Path) -> None:
    triplets = build_triplets([_case()], _CORPUS)
    captured: dict[str, object] = {}

    def stub_train(
        base_model: str, ts: list[Triplet], output_dir: str, config: TrainConfig
    ) -> None:
        captured["base_model"] = base_model
        captured["n_triplets"] = len(ts)
        Path(output_dir, "model.sentinel").write_text("trained", encoding="utf-8")

    out = tmp_path / "candidate"
    result = train_embedding(triplets, out, train_fn=stub_train)

    assert result.base_model == DEFAULT_BASE_MODEL
    assert result.triplets_count == 1
    assert result.output_dir == str(out)
    assert (out / "model.sentinel").read_text(encoding="utf-8") == "trained"
    assert captured["n_triplets"] == 1


def test_train_embedding_rejects_empty_triplets(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one triplet"):
        train_embedding([], tmp_path / "candidate", train_fn=lambda *a: None)


def test_train_embedding_passes_config_through(tmp_path: Path) -> None:
    triplets = build_triplets([_case()], _CORPUS)
    seen: dict[str, TrainConfig] = {}

    def stub_train(
        base_model: str, ts: list[Triplet], output_dir: str, config: TrainConfig
    ) -> None:
        seen["config"] = config

    cfg = TrainConfig(lr=1e-4, batch_size=8, epochs=1)
    train_embedding(triplets, tmp_path / "c", config=cfg, train_fn=stub_train)
    assert seen["config"].lr == 1e-4
    assert seen["config"].epochs == 1


def test_train_config_to_dict_round_trips() -> None:
    cfg = TrainConfig(lr=2e-5, batch_size=16, epochs=3, negatives_per_query=4)
    d = cfg.to_dict()
    assert d["lr"] == 2e-5
    assert d["loss"] == "info_nce"
    assert d["negatives_per_query"] == 4


# ---------------------------------------------------------------------------
# _normalize_nomic_checkpoint — fixes the doubled-prefix save/reload bug
# ---------------------------------------------------------------------------


def _write_safetensors(path: Path, keys: list[str]) -> None:
    torch = pytest.importorskip("torch")
    from safetensors.torch import save_file

    save_file({k: torch.zeros(2) for k in keys}, path, metadata={"format": "pt"})


def test_normalize_nomic_checkpoint_dedoubles_prefix(tmp_path: Path) -> None:
    pytest.importorskip("safetensors")
    from safetensors import safe_open

    from helix.rag.trainer.train import _normalize_nomic_checkpoint

    shard = tmp_path / "model.safetensors"
    _write_safetensors(
        shard,
        [
            "embeddings.word_embeddings.weight",  # untouched
            "emb_ln.weight",  # untouched
            "encoder.encoder.layers.0.attn.Wqkv.weight",  # de-doubled
            "encoder.encoder.layers.1.mlp.fc2.weight",  # de-doubled
        ],
    )

    rewritten = _normalize_nomic_checkpoint(str(tmp_path))

    assert rewritten == 2
    with safe_open(shard, framework="pt") as handle:
        keys = set(handle.keys())
    assert keys == {
        "embeddings.word_embeddings.weight",
        "emb_ln.weight",
        "encoder.layers.0.attn.Wqkv.weight",
        "encoder.layers.1.mlp.fc2.weight",
    }


def test_normalize_nomic_checkpoint_is_noop_when_already_canonical(tmp_path: Path) -> None:
    pytest.importorskip("safetensors")
    from helix.rag.trainer.train import _normalize_nomic_checkpoint

    _write_safetensors(
        tmp_path / "model.safetensors",
        ["encoder.layers.0.attn.Wqkv.weight", "emb_ln.bias"],
    )
    # Already-correct checkpoints (or a future fixed library) are left untouched.
    assert _normalize_nomic_checkpoint(str(tmp_path)) == 0


# ---------------------------------------------------------------------------
# embedding_jobs persistence
# ---------------------------------------------------------------------------


async def test_embedding_job_lifecycle(tmp_path: Path) -> None:
    async with SqliteStore(tmp_path / "helix.db") as store:
        job = await store.create_embedding_job(
            DEFAULT_BASE_MODEL, TrainConfig().to_dict(), job_id="job-1"
        )
        assert job.status == "queued"
        assert job.promoted_at is None

        await store.update_embedding_job("job-1", status="training", triplets_count=42)
        mid = await store.get_embedding_job("job-1")
        assert mid is not None
        assert mid.status == "training"
        assert mid.triplets_count == 42

        metrics = {"before": {"recall@10": 0.66}, "after": {"recall@10": 0.71}}
        await store.update_embedding_job(
            "job-1", status="promoted", metrics=metrics, artifact_uri="data/models/job-1"
        )
        done = await store.get_embedding_job("job-1")
        assert done is not None
        assert done.status == "promoted"
        assert done.metrics == metrics
        assert done.artifact_uri == "data/models/job-1"
        assert done.promoted_at is not None  # auto-set on promotion


async def test_get_embedding_job_missing_returns_none(tmp_path: Path) -> None:
    async with SqliteStore(tmp_path / "helix.db") as store:
        assert await store.get_embedding_job("nope") is None
