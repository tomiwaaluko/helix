"""Contrastive triplet construction for the embedding fine-tune loop.

Turns mined ``FailureCase`` records into ``(query, gold_passage, hard_negatives)``
triplets for InfoNCE / MultipleNegativesRankingLoss training.

The Nomic task prefixes (``search_query:`` / ``search_document:``) are applied
here so the training distribution matches inference exactly — the same prefixes
``Embedder`` uses at retrieval time. They live in one place (``embedder.py``);
this module imports them rather than re-spelling the literals.

Pure and embedding-free: takes ``FailureCase`` objects plus the corpus
(``doc_id -> text``) and returns dataclasses. No I/O, no model loading.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from helix.rag.miner.miner import FailureCase
from helix.tools.embedder import DOCUMENT_PREFIX, QUERY_PREFIX


@dataclass(frozen=True)
class Triplet:
    """One training example: an anchor query, its gold passage, and hard negatives.

    All three carry the Nomic task prefix already (``search_query:`` for the
    query, ``search_document:`` for passages). ``negatives`` may be empty only if
    the caller explicitly allows it; ``build_triplets`` drops such cases by
    default since MultipleNegativesRankingLoss benefits from an explicit hard
    negative (in-batch negatives still apply on top).
    """

    query: str
    positive: str
    negatives: tuple[str, ...]

    def to_texts(self) -> list[str]:
        """``[anchor, positive, *negatives]`` — the InputExample text order for MNRL."""
        return [self.query, self.positive, *self.negatives]


def build_triplets(
    cases: Iterable[FailureCase],
    corpus: dict[str, str],
    *,
    negatives_per_query: int = 4,
    require_negative: bool = True,
) -> list[Triplet]:
    """Build prefixed contrastive triplets from failure cases.

    Args:
        cases:               Mined ``FailureCase`` records (from the miner).
        corpus:              ``{doc_id: text}`` for hard-negative passage lookup.
        negatives_per_query: Cap on hard negatives attached per triplet.
        require_negative:    Drop cases with no resolvable hard negative (default).

    A case is skipped when its gold passage text is empty (the gold doc was not
    in the corpus) — it cannot serve as a positive. Hard negatives that are not
    in the corpus are silently dropped.
    """
    triplets: list[Triplet] = []
    for case in cases:
        if not case.gold_text.strip():
            continue
        negatives = tuple(
            DOCUMENT_PREFIX + corpus[doc_id]
            for doc_id in case.hard_negatives[:negatives_per_query]
            if doc_id in corpus and corpus[doc_id].strip()
        )
        if require_negative and not negatives:
            continue
        triplets.append(
            Triplet(
                query=QUERY_PREFIX + case.query,
                positive=DOCUMENT_PREFIX + case.gold_text,
                negatives=negatives,
            )
        )
    return triplets
