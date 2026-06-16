"""Unit tests for the failure miner and signature classifier.

All tests run against synthetic spans/eval-results — no disk I/O, no network,
no real eval run required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from helix.eval.corpus import load_corpus
from helix.eval.harness import Example
from helix.rag.miner.miner import FailureCase, mine_failures, to_store_row
from helix.rag.miner.signatures import classify
from helix.runtime.sqlite_store import SqliteStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_GOLD_DOC_A = "wiki_aaaa"
_GOLD_DOC_B = "wiki_bbbb"
_WRONG_DOC_C = "wiki_cccc"
_WRONG_DOC_D = "wiki_dddd"

_CORPUS: dict[str, str] = {
    _GOLD_DOC_A: "Scott Derrickson was born in the United States of America.",
    _GOLD_DOC_B: "Ed Wood was an American filmmaker and actor.",
    _WRONG_DOC_C: "This is an unrelated paragraph about something else entirely.",
    _WRONG_DOC_D: "Another distractor passage with some overlapping words.",
}

_QUESTION = "Were Scott Derrickson and Ed Wood of the same nationality?"

_EXAMPLE_FAIL = Example(
    id="ex_001",
    input={"question": _QUESTION},
    expected_output={
        "answer": "yes",
        "supporting_facts": [
            {"doc_id": _GOLD_DOC_A, "sent": 0},
            {"doc_id": _GOLD_DOC_B, "sent": 0},
        ],
    },
    metadata={"hops": 2, "type": "comparison"},
)

_EXAMPLE_PASS = Example(
    id="ex_002",
    input={"question": "Where was Abraham Lincoln born?"},
    expected_output={
        "answer": "Kentucky",
        "supporting_facts": [{"doc_id": _GOLD_DOC_A, "sent": 0}],
    },
    metadata={},
)


def _make_spans(
    question: str,
    trace_id: str,
    *,
    retrieved_docs: list[str],
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Minimal workflow + retrieve span pair."""
    workflow_span: dict[str, Any] = {
        "trace_id": trace_id,
        "span_id": "span_workflow",
        "parent_span_id": None,
        "name": "deep_research",
        "kind": "workflow",
        "start_time": "2026-06-11T10:00:00+00:00",
        "end_time": "2026-06-11T10:00:10+00:00",
        "attributes": {"question": question},
    }
    retrieval_span: dict[str, Any] = {
        "trace_id": trace_id,
        "span_id": "span_retrieve",
        "parent_span_id": "span_workflow",
        "name": "retrieve",
        "kind": "retrieval",
        "start_time": "2026-06-11T10:00:01+00:00",
        "end_time": "2026-06-11T10:00:09+00:00",
        "attributes": {
            "query": query or question,
            "retriever": "hybrid+reranked",
            "top_k": 10,
            "results": [
                {"chunk_id": f"{doc}#chunk0", "doc_id": doc, "score": 0.9 - i * 0.1}
                for i, doc in enumerate(retrieved_docs)
            ],
        },
    }
    return [workflow_span, retrieval_span]


def _make_eval_result_rows(example_id: str, score: float, eval_id: str = "eval-1") -> list[Any]:
    from helix.runtime.sqlite_store import EvalResultRow

    return [
        EvalResultRow(
            id="r1",
            eval_id=eval_id,
            example_id=example_id,
            scorer="retrieval_recall@10",
            score=score,
            details=None,
            created_at="2026-06-11T10:00:00+00:00",
        )
    ]


# ---------------------------------------------------------------------------
# Signature classifier
# ---------------------------------------------------------------------------


def test_classify_lexical_only() -> None:
    # Gold passage shares zero content tokens with query.
    sig = classify(
        query="xenophobia bureaucracy",
        gold_text="photosynthesis chlorophyll ultraviolet",
        retrieved_doc_ids=[_WRONG_DOC_C],
        all_gold_doc_ids={_GOLD_DOC_A},
        missed_gold_doc_ids={_GOLD_DOC_A},
    )
    assert sig == "lexical_only"


def test_classify_semantic_mismatch() -> None:
    # Query and gold share "nationality" but retriever still missed it.
    sig = classify(
        query="nationality American filmmaker",
        gold_text="American filmmaker Ed Wood nationality",
        retrieved_doc_ids=[_WRONG_DOC_C],
        all_gold_doc_ids={_GOLD_DOC_A},
        missed_gold_doc_ids={_GOLD_DOC_A},
    )
    assert sig == "semantic_mismatch"


def test_classify_multi_hop_miss() -> None:
    # Two gold docs; one retrieved (hop 1 succeeded), one missed (hop 2 failed).
    sig = classify(
        query="nationality filmmaker born",
        gold_text="irrelevant for this test",
        retrieved_doc_ids=[_GOLD_DOC_A, _WRONG_DOC_C],
        all_gold_doc_ids={_GOLD_DOC_A, _GOLD_DOC_B},
        missed_gold_doc_ids={_GOLD_DOC_B},
    )
    assert sig == "multi_hop_miss"


def test_classify_multi_hop_requires_partial_retrieval() -> None:
    # All gold missed → not a multi-hop; classify as lexical or semantic.
    sig = classify(
        query="xenophobia bureaucracy",
        gold_text="photosynthesis chlorophyll",
        retrieved_doc_ids=[_WRONG_DOC_C],
        all_gold_doc_ids={_GOLD_DOC_A, _GOLD_DOC_B},
        missed_gold_doc_ids={_GOLD_DOC_A, _GOLD_DOC_B},
    )
    assert sig != "multi_hop_miss"


# ---------------------------------------------------------------------------
# mine_failures — basic case
# ---------------------------------------------------------------------------


def test_mine_failures_produces_one_case_per_missed_gold() -> None:
    # Retrieved only _WRONG_DOC_C and _WRONG_DOC_D; both gold docs missed.
    spans = _make_spans(_QUESTION, "trace-001", retrieved_docs=[_WRONG_DOC_C, _WRONG_DOC_D])
    eval_results = _make_eval_result_rows("ex_001", score=0.0)
    cases = mine_failures(
        [_EXAMPLE_FAIL],
        eval_results,
        _CORPUS,
        spans=spans,
        max_hard_negatives=2,
    )
    assert len(cases) == 2  # one per missed gold doc
    gold_ids = {c.gold_doc_id for c in cases}
    assert gold_ids == {_GOLD_DOC_A, _GOLD_DOC_B}


def test_mine_failures_skips_passed_examples() -> None:
    # Score of 1.0 → no failure to mine.
    spans = _make_spans(_QUESTION, "trace-001", retrieved_docs=[_GOLD_DOC_A, _GOLD_DOC_B])
    eval_results = _make_eval_result_rows("ex_001", score=1.0)
    cases = mine_failures([_EXAMPLE_FAIL], eval_results, _CORPUS, spans=spans)
    assert cases == []


def test_mine_failures_partial_recall() -> None:
    # Only _GOLD_DOC_A retrieved; _GOLD_DOC_B missed → exactly one FailureCase.
    spans = _make_spans(_QUESTION, "trace-001", retrieved_docs=[_GOLD_DOC_A, _WRONG_DOC_C])
    eval_results = _make_eval_result_rows("ex_001", score=0.5)
    cases = mine_failures([_EXAMPLE_FAIL], eval_results, _CORPUS, spans=spans)
    assert len(cases) == 1
    assert cases[0].gold_doc_id == _GOLD_DOC_B
    assert cases[0].example_id == "ex_001"


def test_mine_failures_attaches_hard_negatives() -> None:
    spans = _make_spans(
        _QUESTION,
        "trace-001",
        retrieved_docs=[_WRONG_DOC_C, _WRONG_DOC_D],
    )
    eval_results = _make_eval_result_rows("ex_001", score=0.0)
    cases = mine_failures([_EXAMPLE_FAIL], eval_results, _CORPUS, spans=spans, max_hard_negatives=1)
    for case in cases:
        assert len(case.hard_negatives) == 1
        assert case.hard_negatives[0] not in {_GOLD_DOC_A, _GOLD_DOC_B}


def test_mine_failures_no_spans_for_example() -> None:
    # Spans belong to a different question → no match → no cases.
    spans = _make_spans("A completely different question?", "trace-999", retrieved_docs=[])
    eval_results = _make_eval_result_rows("ex_001", score=0.0)
    cases = mine_failures([_EXAMPLE_FAIL], eval_results, _CORPUS, spans=spans)
    assert cases == []


def test_mine_failures_signature_attached() -> None:
    spans = _make_spans(
        _QUESTION,
        "trace-001",
        retrieved_docs=[_WRONG_DOC_C, _WRONG_DOC_D],
        query="same nationality American filmmaker",
    )
    eval_results = _make_eval_result_rows("ex_001", score=0.0)
    cases = mine_failures([_EXAMPLE_FAIL], eval_results, _CORPUS, spans=spans)
    for case in cases:
        assert case.signature in (
            "lexical_only",
            "semantic_mismatch",
            "multi_hop_miss",
            "ambiguous",
        )


# ---------------------------------------------------------------------------
# to_store_row and SqliteStore.save_failure_cases / get_failure_cases
# ---------------------------------------------------------------------------


def _make_failure_case(gold_doc_id: str = _GOLD_DOC_A) -> FailureCase:
    return FailureCase(
        example_id="ex_001",
        span_id="span_retrieve",
        query=_QUESTION,
        gold_doc_id=gold_doc_id,
        gold_text=_CORPUS[gold_doc_id],
        retrieved_top_k=[{"doc_id": _WRONG_DOC_C, "score": 0.9, "rank": 1}],
        hard_negatives=[_WRONG_DOC_C],
        signature="lexical_only",
    )


async def test_save_and_retrieve_failure_cases(tmp_path: Path) -> None:
    case_a = _make_failure_case(_GOLD_DOC_A)
    case_b = _make_failure_case(_GOLD_DOC_B)
    row_a = to_store_row(case_a)
    row_b = to_store_row(case_b)

    async with SqliteStore(tmp_path / "helix.db") as store:
        await store.save_failure_cases([row_a, row_b])
        all_rows = await store.get_failure_cases()
        assert len(all_rows) == 2
        assert {r.gold_doc_id for r in all_rows} == {_GOLD_DOC_A, _GOLD_DOC_B}

        by_sig = await store.get_failure_cases(signature="lexical_only")
        assert len(by_sig) == 2

        empty = await store.get_failure_cases(signature="multi_hop_miss")
        assert empty == []


async def test_save_failure_cases_is_idempotent(tmp_path: Path) -> None:
    row = to_store_row(_make_failure_case(), case_id="fixed-id")
    async with SqliteStore(tmp_path / "helix.db") as store:
        await store.save_failure_cases([row, row])  # duplicate insert
        all_rows = await store.get_failure_cases()
        assert len(all_rows) == 1  # ON CONFLICT DO NOTHING


async def test_retrieved_top_k_round_trips(tmp_path: Path) -> None:
    case = _make_failure_case()
    row = to_store_row(case)
    async with SqliteStore(tmp_path / "helix.db") as store:
        await store.save_failure_cases([row])
        (loaded,) = await store.get_failure_cases()
    assert loaded.retrieved_top_k == case.retrieved_top_k


# ---------------------------------------------------------------------------
# load_corpus helper
# ---------------------------------------------------------------------------


def test_load_corpus(tmp_path: Path) -> None:
    import json

    p = tmp_path / "corpus.jsonl"
    p.write_text(
        "\n".join(
            json.dumps({"id": doc_id, "text": text, "source": "test", "title": doc_id})
            for doc_id, text in _CORPUS.items()
        ),
        encoding="utf-8",
    )
    loaded = load_corpus(p)
    assert loaded == _CORPUS
