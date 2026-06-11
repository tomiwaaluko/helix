"""End-to-end CLI test: ``index`` then ``eval`` through the full pipeline.

Drives the real Click commands and the real wiring (chunk → embed → Qdrant +
BM25 → hybrid retrieve → deep_research → scorers → JSON report), substituting
stub model components via the CLI's factory hooks. Qdrant runs in on-disk local
mode so the collection written by ``index`` survives into the separate ``eval``
invocation, exactly as a Qdrant server would across two processes.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("qdrant_client")

from click.testing import CliRunner  # noqa: E402

import helix.cli as cli_mod  # noqa: E402
from helix.cli import cli  # noqa: E402
from helix.tools.embedder import Embedder  # noqa: E402
from helix.tools.reranker import Reranker  # noqa: E402

_DIM = 8
_DOCS = [
    {"id": "d0", "text": "the quick brown fox jumps over the lazy dog", "source": "s"},
    {"id": "d1", "text": "apple varieties include gala fuji and honeycrisp", "source": "s"},
    {"id": "d2", "text": "apple pie is baked with cinnamon and sugar", "source": "s"},
    {"id": "d3", "text": "the history of ancient rome and its emperors", "source": "s"},
]

_DATASET = [
    {
        "id": "ex_001",
        "input": {"question": "Tell me about apple varieties"},
        "expected_output": {
            "answer": "Apples are a fruit.",
            "supporting_facts": [{"doc_id": "d1", "sent": 0}, {"doc_id": "d2", "sent": 0}],
        },
    },
    {
        "id": "ex_002",
        "input": {"question": "How is apple pie made"},
        "expected_output": {
            "answer": "Apples are a fruit.",
            "supporting_facts": [{"doc_id": "d2", "sent": 0}],
        },
    },
]


def _words(text: str) -> int:
    return len(text.split())


def _encode(texts: list[str], batch_size: int) -> list[list[float]]:
    return [[float(sum(map(ord, t)) % 13 + 1), *([1.0] * (_DIM - 1))] for t in texts]


def _overlap(pairs: list[tuple[str, str]]) -> list[float]:
    return [float(len(set(q.lower().split()) & set(p.lower().split()))) for q, p in pairs]


class _FakeLLM:
    async def __call__(self, **kwargs: Any) -> SimpleNamespace:
        messages = kwargs["messages"]
        system, user = messages[0]["content"], messages[1]["content"]
        if "decompose" in system.lower():
            content = json.dumps(["apple varieties", "apple pie ingredients"])
        else:
            cited = [doc["id"] for doc in _DOCS if f"[doc_id={doc['id']}]" in user]
            content = "Apples are a fruit.\nCITATIONS: " + ", ".join(cited)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )


# Embedded on-disk Qdrant dir, shared across the index/eval invocations via
# --qdrant-path (exercises the real _build_adapter local-mode branch).
_QDRANT_PATH = "qdrant_local"


def _patch_factories(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the model-backed components are stubbed; the Qdrant adapter is the real
    # one, reached through --qdrant-path (embedded, no server).
    monkeypatch.setattr(
        cli_mod,
        "_build_embedder",
        lambda span_logger: Embedder(span_logger=span_logger, encode_fn=_encode),
    )
    monkeypatch.setattr(cli_mod, "_build_reranker", lambda: Reranker(predict_fn=_overlap))
    monkeypatch.setattr(cli_mod, "_build_completion_fns", lambda: (_FakeLLM(), lambda _raw: 0.0))
    monkeypatch.setattr(cli_mod, "_token_counter", lambda: _words)


def test_cli_index_then_eval(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("corpus.jsonl").write_text("\n".join(json.dumps(d) for d in _DOCS), encoding="utf-8")
        Path("dataset.jsonl").write_text(
            "\n".join(json.dumps(e) for e in _DATASET), encoding="utf-8"
        )
        _patch_factories(monkeypatch)

        index_result = runner.invoke(
            cli,
            [
                "index",
                "--corpus",
                "corpus.jsonl",
                "--collection",
                "corpus.base",
                "--qdrant-path",
                _QDRANT_PATH,
                "--vector-size",
                str(_DIM),
                "--bm25",
                "bm25.pkl",
                "--spans",
                "spans.jsonl",
            ],
        )
        assert index_result.exit_code == 0, index_result.output
        assert "Indexed 4 docs" in index_result.output
        assert Path("bm25.pkl").exists()

        eval_result = runner.invoke(
            cli,
            [
                "eval",
                "--workflow",
                "deep_research",
                "--dataset",
                "dataset.jsonl",
                "--scorers",
                "answer_f1,citation_precision,retrieval_recall@10",
                "--concurrency",
                "2",
                "--output",
                "report.json",
                "--qdrant-path",
                _QDRANT_PATH,
                "--bm25",
                "bm25.pkl",
                "--top-k",
                "2",
                "--no-cache",
                "--spans",
                "spans.jsonl",
            ],
        )
        assert eval_result.exit_code == 0, eval_result.output

        report = json.loads(Path("report.json").read_text(encoding="utf-8"))
        assert report["workflow"] == "deep_research"
        assert report["dataset"] == "dataset"
        assert report["n"] == 2
        assert set(report["metrics"]) == {
            "answer_f1",
            "citation_precision",
            "retrieval_recall@10",
        }
        for agg in report["metrics"].values():
            assert {"mean", "std", "ci_low", "ci_high", "n"} <= set(agg)
            assert agg["n"] == 2
        # The fake LLM echoes the gold answer, so F1 is perfect.
        assert report["metrics"]["answer_f1"]["mean"] == 1.0
        # Both apple docs are retrievable; recall should be positive.
        assert report["metrics"]["retrieval_recall@10"]["mean"] > 0.0
        assert "timestamp" in report


def test_cli_eval_rejects_unknown_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("dataset.jsonl").write_text(json.dumps(_DATASET[0]), encoding="utf-8")
        result = runner.invoke(
            cli,
            ["eval", "--workflow", "nope", "--dataset", "dataset.jsonl"],
        )
        assert result.exit_code != 0
        assert "unknown workflow" in result.output


def test_cli_run_single_question(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("corpus.jsonl").write_text("\n".join(json.dumps(d) for d in _DOCS), encoding="utf-8")
        _patch_factories(monkeypatch)

        index_result = runner.invoke(
            cli,
            [
                "index",
                "--corpus",
                "corpus.jsonl",
                "--qdrant-path",
                _QDRANT_PATH,
                "--vector-size",
                str(_DIM),
                "--bm25",
                "bm25.pkl",
                "--spans",
                "spans.jsonl",
            ],
        )
        assert index_result.exit_code == 0, index_result.output

        run_result = runner.invoke(
            cli,
            [
                "run",
                "--input",
                json.dumps({"question": "Tell me about apples"}),
                "--qdrant-path",
                _QDRANT_PATH,
                "--bm25",
                "bm25.pkl",
                "--top-k",
                "2",
                "--no-cache",
                "--spans",
                "spans.jsonl",
            ],
        )
        assert run_result.exit_code == 0, run_result.output
        assert "Apples are a fruit." in run_result.output


# ---------------------------------------------------------------------------
# finetune — full loop (mine → train → promote) with stubbed heavy backends
# ---------------------------------------------------------------------------

# A train example whose gold doc (d3, ancient Rome) is never retrieved: the fake
# LLM always decomposes into apple sub-queries, so apple docs come back and d3 is
# missed — exactly the recall<1.0 failure the miner is meant to catch.
_TRAIN_DATASET = [
    {
        "id": "tr_001",
        "input": {"question": "Tell me about apple varieties"},
        "expected_output": {
            "answer": "Rome was an empire.",
            "supporting_facts": [{"doc_id": "d3", "sent": 0}],
        },
    },
]


def _patch_finetune_backends(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the training fit and the promotion canary so no model/Qdrant is needed.

    The candidate "wins" the canary (finds dev gold; baseline does not), so the
    loop reaches the promoted terminal state. Returns a dict the test inspects.
    """
    captured: dict[str, Any] = {"trained": False, "indexed": []}

    def _stub_train_fn(base_model: str, triplets: Any, output_dir: str, config: Any) -> None:
        captured["trained"] = True
        captured["n_triplets"] = len(triplets)
        Path(output_dir, "model.sentinel").write_text("trained", encoding="utf-8")

    monkeypatch.setattr(cli_mod, "_train_backend", lambda: _stub_train_fn)

    def _build_backends(
        *,
        checkpoint_dir: str,
        adapter: Any,
        candidate_collection: str,
        bm25_path: str,
        top_k: int,
        span_logger: Any,
    ) -> tuple[Any, Any]:
        async def _index(corpus_path: str, collection: str) -> None:
            captured["indexed"].append((corpus_path, collection))
            # Create the (empty) collection so the real alias swap on promotion resolves.
            await adapter.create_collection(collection, vector_size=_DIM)

        async def _retrieve(query: str, collection: str, k: int) -> list[str]:
            # Baseline (corpus.active) misses dev gold; candidate collection finds d1.
            return ["d_other"] if collection == "corpus.active" else ["d1"]

        return _index, _retrieve

    monkeypatch.setattr(cli_mod, "_build_promotion_backends", _build_backends)
    return captured


def test_cli_finetune_full_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("corpus.jsonl").write_text("\n".join(json.dumps(d) for d in _DOCS), encoding="utf-8")
        Path("train.jsonl").write_text(
            "\n".join(json.dumps(e) for e in _TRAIN_DATASET), encoding="utf-8"
        )
        Path("eval.jsonl").write_text("\n".join(json.dumps(e) for e in _DATASET), encoding="utf-8")
        _patch_factories(monkeypatch)
        captured = _patch_finetune_backends(monkeypatch)

        index_result = runner.invoke(
            cli,
            [
                "index",
                "--corpus",
                "corpus.jsonl",
                "--collection",
                "corpus.base",
                "--qdrant-path",
                _QDRANT_PATH,
                "--vector-size",
                str(_DIM),
                "--bm25",
                "bm25.pkl",
                "--spans",
                "spans.jsonl",
            ],
        )
        assert index_result.exit_code == 0, index_result.output

        result = runner.invoke(
            cli,
            [
                "finetune",
                "--train",
                "train.jsonl",
                "--eval",
                "eval.jsonl",
                "--corpus",
                "corpus.jsonl",
                "--concurrency",
                "1",
                "--qdrant-path",
                _QDRANT_PATH,
                "--bm25",
                "bm25.pkl",
                "--top-k",
                "2",
                "--epochs",
                "1",
                "--batch-size",
                "2",
                "--db",
                "helix.db",
                "--no-cache",
                "--spans",
                "spans.jsonl",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "promoted" in result.output
        assert captured["trained"] is True
        # At least one failure (the missed d3 gold) became a training triplet.
        assert captured["n_triplets"] >= 1
        assert "failures mined: 1" in result.output
        # The candidate collection was indexed during promotion.
        assert captured["indexed"], "promotion index_fn was not called"


def test_cli_finetune_archives_when_no_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """When every train example retrieves all its gold docs, there is nothing to mine."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("corpus.jsonl").write_text("\n".join(json.dumps(d) for d in _DOCS), encoding="utf-8")
        # Gold docs are the apple docs the fake pipeline reliably retrieves → recall 1.0.
        Path("train.jsonl").write_text(json.dumps(_DATASET[1]), encoding="utf-8")
        Path("eval.jsonl").write_text(json.dumps(_DATASET[1]), encoding="utf-8")
        _patch_factories(monkeypatch)
        captured = _patch_finetune_backends(monkeypatch)

        index_result = runner.invoke(
            cli,
            [
                "index",
                "--corpus",
                "corpus.jsonl",
                "--qdrant-path",
                _QDRANT_PATH,
                "--vector-size",
                str(_DIM),
                "--bm25",
                "bm25.pkl",
                "--spans",
                "spans.jsonl",
            ],
        )
        assert index_result.exit_code == 0, index_result.output

        result = runner.invoke(
            cli,
            [
                "finetune",
                "--train",
                "train.jsonl",
                "--eval",
                "eval.jsonl",
                "--corpus",
                "corpus.jsonl",
                "--concurrency",
                "1",
                "--qdrant-path",
                _QDRANT_PATH,
                "--bm25",
                "bm25.pkl",
                "--top-k",
                "2",
                "--db",
                "helix.db",
                "--no-cache",
                "--spans",
                "spans.jsonl",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "no_failures" in result.output
        assert captured["trained"] is False


def test_cli_finetune_reports_skipped_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the mining eval skips examples, failures_skipped appears in the CLI output."""

    import helix.eval.harness as harness_mod

    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("corpus.jsonl").write_text("\n".join(json.dumps(d) for d in _DOCS), encoding="utf-8")
        Path("train.jsonl").write_text(
            "\n".join(json.dumps(e) for e in _TRAIN_DATASET), encoding="utf-8"
        )
        Path("eval.jsonl").write_text("\n".join(json.dumps(e) for e in _DATASET), encoding="utf-8")
        _patch_factories(monkeypatch)
        _patch_finetune_backends(monkeypatch)

        index_result = runner.invoke(
            cli,
            [
                "index",
                "--corpus",
                "corpus.jsonl",
                "--collection",
                "corpus.base",
                "--qdrant-path",
                _QDRANT_PATH,
                "--vector-size",
                str(_DIM),
                "--bm25",
                "bm25.pkl",
                "--spans",
                "spans.jsonl",
            ],
        )
        assert index_result.exit_code == 0, index_result.output

        # Wrap `evaluate` to inject `examples_skipped=1` on the first (mining) call.
        _original_evaluate = harness_mod.evaluate

        async def _patched_evaluate(*args: Any, **kwargs: Any) -> Any:
            report = await _original_evaluate(*args, **kwargs)
            report.examples_skipped = 1  # simulate one provider-error skip
            return report

        monkeypatch.setattr(cli_mod, "evaluate", _patched_evaluate)

        result = runner.invoke(
            cli,
            [
                "finetune",
                "--train",
                "train.jsonl",
                "--eval",
                "eval.jsonl",
                "--corpus",
                "corpus.jsonl",
                "--concurrency",
                "1",
                "--qdrant-path",
                _QDRANT_PATH,
                "--bm25",
                "bm25.pkl",
                "--top-k",
                "2",
                "--epochs",
                "1",
                "--batch-size",
                "2",
                "--db",
                "helix.db",
                "--no-cache",
                "--spans",
                "spans.jsonl",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "examples skipped (provider errors): 1" in result.output


def test_promotion_hybrid_retrieve_fn_routes_and_dedupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real canary retrieve_fn runs the full hybrid pipeline against the right collection.

    Exercises ``_build_promotion_backends`` end-to-end (HybridRetriever + RRF + rerank)
    over real embedded Qdrant + BM25 — only the embedder model load is stubbed — and
    verifies the active vs candidate routing and doc-level dedup.
    """
    import asyncio

    import helix.cli as c
    from helix.rag.chunker import chunk_document
    from helix.rag.indexer import index_corpus
    from helix.tools.bm25 import BM25Index
    from helix.tools.embedder import Embedder
    from helix.tools.qdrant_adapter import QdrantAdapter

    pytest.importorskip("qdrant_client")
    from qdrant_client import AsyncQdrantClient

    monkeypatch.setattr(
        c, "_build_embedder", lambda sl: Embedder(span_logger=sl, encode_fn=_encode)
    )
    monkeypatch.setattr(
        c,
        "_build_candidate_embedder",
        lambda ckpt, sl: Embedder(span_logger=sl, encode_fn=_encode),
    )
    monkeypatch.setattr(c, "_build_reranker", lambda: Reranker(predict_fn=_overlap))

    async def _drive() -> tuple[list[str], list[str]]:
        embedder = Embedder(encode_fn=_encode)
        chunks = [
            chunk
            for d in _DOCS
            for chunk in chunk_document(d["id"], d["text"], d["source"], token_counter=_words)
        ]
        BM25Index.build(chunks).save("bm25.pkl")
        adapter = QdrantAdapter(client=AsyncQdrantClient(path="qdrant_promo"))
        # Live collection behind corpus.active, and a separate candidate collection.
        await index_corpus(
            "corpus.jsonl", "corpus.base", embedder=embedder, adapter=adapter, vector_size=_DIM
        )
        await index_corpus(
            "corpus.jsonl",
            "corpus.candidate.jobX",
            embedder=embedder,
            adapter=adapter,
            alias="corpus.candidate.jobX",
            vector_size=_DIM,
        )
        _, retrieve_fn = c._build_promotion_backends(
            checkpoint_dir="ckpt",
            adapter=adapter,
            candidate_collection="corpus.candidate.jobX",
            bm25_path="bm25.pkl",
            top_k=3,
            span_logger=c._span_logger(None),
        )
        assert retrieve_fn is not None
        active = await retrieve_fn("apple varieties", "corpus.active", 3)
        candidate = await retrieve_fn("apple varieties", "corpus.candidate.jobX", 3)
        await adapter.aclose()
        return active, candidate

    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("corpus.jsonl").write_text("\n".join(json.dumps(d) for d in _DOCS), encoding="utf-8")
        active_hits, candidate_hits = asyncio.run(_drive())

    # Both arms return real doc_ids from the corpus, deduped (no repeats).
    for hits in (active_hits, candidate_hits):
        assert hits, "hybrid retrieve returned nothing"
        assert len(hits) == len(set(hits)), "doc_ids were not deduped"
        assert all(h in {d["id"] for d in _DOCS} for h in hits)
