"""Rule-based failure-signature classifier for the embedding fine-tune loop.

The four signatures mirror the target-state taxonomy in ``docs/architecture.md``:

- ``lexical_only``      — query and gold passage share no surface tokens; the
                          embedding had to bridge a purely lexical gap (no help
                          from token overlap).
- ``semantic_mismatch`` — some token overlap exists but the retriever still
                          missed the gold passage; the embedding model failed to
                          rank it highly enough despite a lexical signal.
- ``multi_hop_miss``    — the example has ≥2 gold docs; at least one was
                          retrieved (a hop succeeded) but at least one was
                          missed (a hop failed). The signature isolates the
                          second-hop failure pattern.
- ``ambiguous``         — catch-all; does not fit the patterns above. Typically
                          means the query is underspecified or the gold passage
                          is genuinely hard to distinguish from distractors.

Classification is pure and embedding-free so it can run on any failure case
without I/O; the ``Embedder`` is not imported here.
"""

from __future__ import annotations

import re

_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "was",
        "were",
        "are",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "by",
        "with",
        "from",
        "and",
        "or",
        "but",
        "not",
        "no",
        "it",
        "its",
        "he",
        "she",
        "they",
        "we",
        "i",
        "that",
        "this",
        "which",
        "who",
        "what",
        "where",
        "when",
        "how",
    }
)

_TOKEN_SPLIT = re.compile(r"\w+")

SIGNATURES = ("lexical_only", "semantic_mismatch", "multi_hop_miss", "ambiguous")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_SPLIT.findall(text.lower()) if t not in _STOP and len(t) > 1}


def classify(
    query: str,
    gold_text: str,
    retrieved_doc_ids: list[str],
    all_gold_doc_ids: set[str],
    missed_gold_doc_ids: set[str],
) -> str:
    """Return one of ``SIGNATURES`` for a single (query, missed-gold) failure.

    Args:
        query:              The retrieval sub-query that produced the failure.
        gold_text:          Full text of the missed gold passage.
        retrieved_doc_ids:  Ranked list of retrieved doc_ids (order preserved).
        all_gold_doc_ids:   Every gold doc_id for this example (≥1).
        missed_gold_doc_ids: Gold doc_ids absent from the retrieved set (≥1).
    """
    query_tokens = _tokens(query)
    gold_tokens = _tokens(gold_text)
    overlap = query_tokens & gold_tokens

    # multi-hop: at least one gold doc was retrieved (a hop succeeded) and at
    # least one was missed (a hop failed); example has ≥2 distinct gold docs.
    retrieved_set = set(retrieved_doc_ids)
    retrieved_gold = all_gold_doc_ids & retrieved_set
    if len(all_gold_doc_ids) >= 2 and retrieved_gold and missed_gold_doc_ids:
        return "multi_hop_miss"

    # lexical-only: no surface-token overlap between query and gold passage.
    if not overlap:
        return "lexical_only"

    # semantic_mismatch: there IS overlap but the retriever still missed it —
    # the embedding model failed to surface the passage despite lexical signal.
    if overlap:
        return "semantic_mismatch"

    return "ambiguous"
