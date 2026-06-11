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
