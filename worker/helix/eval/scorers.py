"""Eval scorers: answer F1, citation precision, retrieval recall@k.

Each scorer matches the harness ``Scorer`` signature ``(Example, prediction) ->
float | ScoreResult``, where ``prediction`` is the workflow's ``Answer``.

``retrieval_recall_at_k`` reads ``prediction.metadata["retrieved_doc_ids"]`` —
populated by the deep_research workflow — and is the metric the fine-tuning loop
targets, so it must reflect retrieval *before* any synthesis filtering.
"""

from __future__ import annotations

import re
from typing import Any

from helix.eval.harness import Example, Scorer, ScoreResult

# Standard HotpotQA/SQuAD answer normalization.
_ARTICLES = {"a", "an", "the"}
_TOKEN_SPLIT = re.compile(r"\w+")


def _normalize_tokens(text: str) -> list[str]:
    return [token for token in _TOKEN_SPLIT.findall(text.lower()) if token not in _ARTICLES]


def _gold_doc_ids(example: Example) -> set[str]:
    facts = example.expected_output.get("supporting_facts", [])
    return {str(fact["doc_id"]) for fact in facts}


def _predicted_text(prediction: Any) -> str:
    text = getattr(prediction, "text", prediction)
    return text if isinstance(text, str) else ""


def answer_f1(example: Example, prediction: Any) -> float:
    """Token-level F1 between the predicted and gold answers."""
    gold_tokens = _normalize_tokens(str(example.expected_output.get("answer", "")))
    pred_tokens = _normalize_tokens(_predicted_text(prediction))
    if not gold_tokens or not pred_tokens:
        return 0.0

    gold_counts: dict[str, int] = {}
    for token in gold_tokens:
        gold_counts[token] = gold_counts.get(token, 0) + 1
    overlap = 0
    for token in pred_tokens:
        if gold_counts.get(token, 0) > 0:
            gold_counts[token] -= 1
            overlap += 1
    if overlap == 0:
        return 0.0

    precision = overlap / len(pred_tokens)
    recall = overlap / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def _cited_doc_ids(prediction: Any) -> list[str]:
    citations = getattr(prediction, "citations", [])
    return [str(citation.doc_id) for citation in citations]


def citation_precision(example: Example, prediction: Any) -> ScoreResult:
    """Fraction of cited doc_ids that are gold supporting facts.

    No citations → vacuous precision of 1.0, flagged in the details.
    """
    cited = _cited_doc_ids(prediction)
    gold = _gold_doc_ids(example)
    if not cited:
        return ScoreResult(score=1.0, details={"vacuous": True, "n_citations": 0})
    matched = sum(1 for doc_id in cited if doc_id in gold)
    return ScoreResult(
        score=matched / len(cited),
        details={"vacuous": False, "n_citations": len(cited), "matched": matched},
    )


def _retrieved_doc_ids(prediction: Any, k: int) -> list[str]:
    metadata = getattr(prediction, "metadata", {})
    retrieved = metadata.get("retrieved_doc_ids", []) if isinstance(metadata, dict) else []
    return [str(doc_id) for doc_id in retrieved][:k]


def retrieval_recall_at_k(example: Example, prediction: Any, k: int = 10) -> ScoreResult:
    """Fraction of gold doc_ids found in the top-k retrieved set."""
    gold = _gold_doc_ids(example)
    if not gold:
        return ScoreResult(score=0.0, details={"k": k, "n_gold": 0})
    retrieved = set(_retrieved_doc_ids(prediction, k))
    found = len(gold & retrieved)
    return ScoreResult(
        score=found / len(gold), details={"k": k, "n_gold": len(gold), "found": found}
    )


def retrieval_recall_scorer(k: int = 10) -> Scorer:
    """Bind ``k`` to produce a 2-arg scorer for the harness."""

    def scorer(example: Example, prediction: Any) -> ScoreResult:
        return retrieval_recall_at_k(example, prediction, k=k)

    return scorer


def default_scorers() -> dict[str, Scorer]:
    """The slice's three headline scorers, keyed by name for the harness."""
    return {
        "answer_f1": answer_f1,
        "citation_precision": citation_precision,
        "retrieval_recall@10": retrieval_recall_scorer(10),
    }
