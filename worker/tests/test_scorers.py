"""Scorers: answer F1, citation precision, retrieval recall@k, and edge cases."""

import pytest

from helix.eval.harness import Example, evaluate
from helix.eval.scorers import (
    answer_f1,
    citation_precision,
    default_scorers,
    retrieval_recall_at_k,
)
from helix.types import Answer, Citation


def _example(answer: str = "", supporting: list[str] | None = None) -> Example:
    facts = [{"doc_id": d, "sent": 0} for d in (supporting or [])]
    return Example(
        id="ex",
        input={"question": "q"},
        expected_output={"answer": answer, "supporting_facts": facts},
        metadata={},
    )


def _answer(
    text: str = "", cited: list[str] | None = None, retrieved: list[str] | None = None
) -> Answer:
    return Answer(
        text=text,
        citations=[Citation(doc_id=d, passage="") for d in (cited or [])],
        metadata={"retrieved_doc_ids": list(retrieved or [])},
    )


# ---- answer_f1 --------------------------------------------------------------


def test_answer_f1_exact_match() -> None:
    assert answer_f1(_example("Scott Derrickson"), _answer("Scott Derrickson")) == 1.0


def test_answer_f1_ignores_articles_and_case() -> None:
    assert answer_f1(_example("Eiffel Tower"), _answer("the eiffel TOWER")) == 1.0


def test_answer_f1_partial_overlap() -> None:
    score = answer_f1(_example("Scott Derrickson and Ed Wood"), _answer("Scott Derrickson"))
    # precision 2/2, recall 2/5 -> F1 = 0.5714...
    assert score == pytest.approx(4 / 7)


def test_answer_f1_zero_cases() -> None:
    assert answer_f1(_example("cat"), _answer("dog")) == 0.0  # no overlap
    assert answer_f1(_example("cat"), _answer("")) == 0.0  # empty prediction
    assert answer_f1(_example(""), _answer("cat")) == 0.0  # empty gold


# ---- citation_precision -----------------------------------------------------


def test_citation_precision_all_correct() -> None:
    result = citation_precision(_example(supporting=["w1", "w2"]), _answer(cited=["w1", "w2"]))
    assert result.score == 1.0
    assert result.details["matched"] == 2


def test_citation_precision_partial() -> None:
    result = citation_precision(_example(supporting=["w1"]), _answer(cited=["w1", "w_bad"]))
    assert result.score == 0.5
    assert result.details == {"vacuous": False, "n_citations": 2, "matched": 1}


def test_citation_precision_vacuous_when_no_citations() -> None:
    result = citation_precision(_example(supporting=["w1"]), _answer(cited=[]))
    assert result.score == 1.0
    assert result.details["vacuous"] is True


# ---- retrieval_recall_at_k --------------------------------------------------


def test_retrieval_recall_full_and_partial() -> None:
    example = _example(supporting=["w1", "w2"])
    assert retrieval_recall_at_k(example, _answer(retrieved=["w1", "w2", "w3"])).score == 1.0
    assert retrieval_recall_at_k(example, _answer(retrieved=["w1", "w9"])).score == 0.5


def test_retrieval_recall_respects_k() -> None:
    example = _example(supporting=["w_late"])
    # The gold doc is retrieved but only at rank 3; k=2 excludes it.
    prediction = _answer(retrieved=["a", "b", "w_late"])
    assert retrieval_recall_at_k(example, prediction, k=2).score == 0.0
    assert retrieval_recall_at_k(example, prediction, k=3).score == 1.0


def test_retrieval_recall_no_gold() -> None:
    result = retrieval_recall_at_k(_example(supporting=[]), _answer(retrieved=["a"]))
    assert result.score == 0.0
    assert result.details["n_gold"] == 0


# ---- harness integration ----------------------------------------------------


async def test_default_scorers_plug_into_harness() -> None:
    dataset = [
        Example(
            id="ex_001",
            input={"question": "q1"},
            expected_output={"answer": "Paris", "supporting_facts": [{"doc_id": "w1", "sent": 0}]},
            metadata={},
        )
    ]

    async def runner(_inp: object) -> Answer:
        return _answer("Paris", cited=["w1"], retrieved=["w1"])

    report = await evaluate(runner, dataset, default_scorers())
    assert set(report.metrics) == {"answer_f1", "citation_precision", "retrieval_recall@10"}
    assert report.metrics["answer_f1"]["mean"] == 1.0
    assert report.metrics["citation_precision"]["mean"] == 1.0
    assert report.metrics["retrieval_recall@10"]["mean"] == 1.0
