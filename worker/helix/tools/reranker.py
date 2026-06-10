"""BGE cross-encoder reranker — the second retrieval stage.

Scores each (query, passage) pair with a cross-encoder and returns the top-k.
The reranker stays fixed across embedding experiments so the fine-tune's
contribution is isolated.

The ``sentence_transformers.CrossEncoder`` is lazily loaded and the predict
function is injectable, so unit tests run with a stub instead of the model.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

PredictFn = Callable[[list[tuple[str, str]]], Sequence[float]]


@dataclass
class RankedPassage:
    index: int  # position in the input ``passages`` list
    passage: str
    score: float


class Reranker:
    """Cross-encoder reranker over (query, passage) pairs."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-base",
        *,
        predict_fn: PredictFn | None = None,
    ) -> None:
        self.model_name = model_name
        self._predict = predict_fn

    def _load(self) -> PredictFn:
        from sentence_transformers import CrossEncoder

        model = CrossEncoder(self.model_name)

        def predict(pairs: list[tuple[str, str]]) -> Sequence[float]:
            return cast("Sequence[float]", model.predict(pairs))

        return predict

    def rerank(self, query: str, passages: list[str], top_k: int = 10) -> list[RankedPassage]:
        if not passages:
            return []
        predict = self._predict or self._load()
        scores = predict([(query, passage) for passage in passages])
        order = sorted(range(len(passages)), key=lambda i: scores[i], reverse=True)[:top_k]
        return [RankedPassage(index=i, passage=passages[i], score=float(scores[i])) for i in order]
