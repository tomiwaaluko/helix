"""deep_research: subquery/synthesis parsing and an end-to-end local run."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("qdrant_client")

from qdrant_client import AsyncQdrantClient  # noqa: E402

from helix.logging import SpanLogger  # noqa: E402
from helix.rag.chunker import chunk_document  # noqa: E402
from helix.rag.indexer import index_corpus  # noqa: E402
from helix.rag.retriever import HybridRetriever  # noqa: E402
from helix.tools.bm25 import BM25Index  # noqa: E402
from helix.tools.embedder import Embedder  # noqa: E402
from helix.tools.qdrant_adapter import QdrantAdapter  # noqa: E402
from helix.tools.reranker import Reranker  # noqa: E402
from helix.types import Doc  # noqa: E402
from helix.workflows.deep_research import (  # noqa: E402
    ResearchDeps,
    _parse_subqueries,
    _parse_synthesis,
    deep_research,
    using_research_deps,
)

# ---- parser units -----------------------------------------------------------


def test_parse_subqueries_plain_and_fenced() -> None:
    assert _parse_subqueries('["a", "b"]', "q") == ["a", "b"]
    assert _parse_subqueries('```json\n["a", "b", "c"]\n```', "q") == ["a", "b", "c"]


def test_parse_subqueries_caps_and_falls_back() -> None:
    assert _parse_subqueries('["a","b","c","d","e"]', "q") == ["a", "b", "c", "d"]
    assert _parse_subqueries("not json at all", "the question") == ["the question"]
    assert _parse_subqueries("[]", "the question") == ["the question"]


def test_parse_synthesis_splits_answer_and_filters_citations() -> None:
    evidence = [
        Doc(id="wiki_a", text="A text", source="s", score=0.9),
        Doc(id="wiki_b", text="B text", source="s", score=0.8),
    ]
    answer = _parse_synthesis("Paris is the capital.\nCITATIONS: wiki_a, wiki_zzz", evidence)
    assert answer.text == "Paris is the capital."
    assert [c.doc_id for c in answer.citations] == ["wiki_a"]  # unknown id dropped
    assert answer.citations[0].relevance == 0.9


def test_parse_synthesis_i_dont_know() -> None:
    answer = _parse_synthesis("I don't know\nCITATIONS:", [])
    assert answer.text == "I don't know"
    assert answer.citations == []


# ---- end-to-end -------------------------------------------------------------

_DIM = 8
_DOCS = [
    {"id": "d0", "text": "the quick brown fox jumps over the lazy dog", "source": "s"},
    {"id": "d1", "text": "apple varieties include gala fuji and honeycrisp", "source": "s"},
    {"id": "d2", "text": "apple pie is baked with cinnamon and sugar", "source": "s"},
    {"id": "d3", "text": "the history of ancient rome and its emperors", "source": "s"},
]


def _words(text: str) -> int:
    return len(text.split())


def _encode(texts: list[str], batch_size: int) -> list[list[float]]:
    return [[float(sum(map(ord, t)) % 13 + 1), *([1.0] * (_DIM - 1))] for t in texts]


def _overlap(pairs: list[tuple[str, str]]) -> list[float]:
    return [float(len(set(q.lower().split()) & set(p.lower().split()))) for q, p in pairs]


class FakeResearchLLM:
    """Returns a subquery array for decompose, answer+citations for synthesize."""

    def __init__(self) -> None:
        self.systems: list[str] = []

    async def __call__(self, **kwargs: Any) -> SimpleNamespace:
        messages = kwargs["messages"]
        system, user = messages[0]["content"], messages[1]["content"]
        self.systems.append(system)
        if "decompose" in system.lower():
            content = json.dumps(["apple varieties", "apple pie ingredients"])
        else:
            cited = [doc["id"] for doc in _DOCS if f"[doc_id={doc['id']}]" in user]
            content = "Apples are a fruit.\nCITATIONS: " + ", ".join(cited)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )


async def test_deep_research_local_end_to_end(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text("\n".join(json.dumps(d) for d in _DOCS), encoding="utf-8")
    logger = SpanLogger(tmp_path / "spans.jsonl")
    embedder = Embedder(span_logger=logger, encode_fn=_encode)
    adapter = QdrantAdapter(client=AsyncQdrantClient(location=":memory:"), span_logger=logger)
    fake_llm = FakeResearchLLM()

    async with adapter:
        await index_corpus(
            corpus,
            "corpus.base",
            embedder=embedder,
            adapter=adapter,
            vector_size=_DIM,
            token_counter=_words,
            span_logger=logger,
        )
        chunks = [
            chunk
            for doc in _DOCS
            for chunk in chunk_document(doc["id"], doc["text"], doc["source"], token_counter=_words)
        ]
        retriever = HybridRetriever(
            embedder,
            adapter,
            BM25Index.build(chunks),
            Reranker(predict_fn=_overlap),
            span_logger=logger,
        )
        deps = ResearchDeps(
            retriever=retriever,
            top_k=2,
            span_logger=logger,
            completion_fn=fake_llm,
            cost_fn=lambda _raw: 0.0,
        )
        with using_research_deps(deps):
            answer = await deep_research.local(question="Tell me about apples")

    assert answer.text == "Apples are a fruit."

    retrieved = answer.metadata["retrieved_doc_ids"]
    assert isinstance(retrieved, list)
    assert retrieved == list(dict.fromkeys(retrieved))  # deduped, order preserved
    assert set(retrieved) <= {"d0", "d1", "d2", "d3"}
    assert {"d1", "d2"} & set(retrieved)  # the apple docs were retrieved

    assert {c.doc_id for c in answer.citations} <= set(retrieved)
    # Two LLM calls: one decompose, one synthesize.
    assert sum("decompose" in s.lower() for s in fake_llm.systems) == 1
    assert len(fake_llm.systems) == 2

    spans = [json.loads(line) for line in (tmp_path / "spans.jsonl").read_text().splitlines()]
    workflow_span = next(s for s in spans if s["name"] == "deep_research")
    assert workflow_span["kind"] == "workflow"
    children_kinds = {s["kind"] for s in spans if s["parent_span_id"] == workflow_span["span_id"]}
    assert {"llm", "retrieval"} <= children_kinds
