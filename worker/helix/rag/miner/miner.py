"""Failure miner for the embedding fine-tune loop.

Joins retrieval spans from ``data/spans.jsonl`` to per-example eval results in
SQLite, identifies retrieval failures (examples where recall@10 < 1.0), and
produces ``FailureCase`` records ready for the trainer's triplet builder.

Pipeline
--------
1. Load spans.  Build two indexes keyed by ``trace_id``:
   - ``workflow_spans``:  the root ``deep_research`` span, which carries the
     original question in ``attributes.question``.
   - ``retrieval_spans``: all ``retrieve`` spans for that trace, each carrying
     ``attributes.query`` (the sub-query) and ``attributes.results``
     (ranked ``[{doc_id, chunk_id, score}]``).
2. Join examples to spans.  Index examples by question text
   (``input.question``) and match each workflow span by question.
3. Identify failures.  For each example whose ``recall_scorer`` score in
   ``eval_results`` is < 1.0, collect the missed gold doc_ids
   (``expected_output.supporting_facts[].doc_id`` minus the union of retrieved
   doc_ids across all retrieval spans for that trace).
4. Emit one ``FailureCase`` per missed gold doc.  Use the retrieval sub-query
   whose result set has the highest rank for a gold doc (the "closest miss") as
   the representative query; fall back to the original question when none of the
   sub-queries retrieved any gold doc.
5. Classify the failure signature (pure rules, see ``signatures.py``).
6. Return the list; the caller persists via ``SqliteStore.save_failure_cases``.

The corpus dict (``doc_id → text``) is required to look up gold passage text
(needed for signature classification) and is also attached to each
``FailureCase.gold_text`` for the trainer.

Hard negatives
--------------
Each ``FailureCase`` carries ``hard_negatives``: the top retrieved-but-wrong
doc_ids from the representative retrieval span, capped at
``max_hard_negatives``.  The trainer converts these to passage text via the
same corpus dict.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from typing import Any

import httpx

from helix.eval.harness import Example
from helix.logging import new_id
from helix.rag.miner.signatures import classify
from helix.runtime.sqlite_store import EvalResultRow, FailureCaseRow

_RECALL_SCORER = "retrieval_recall@10"


@dataclass
class FailureCase:
    """A single missed gold passage, ready to become a training triplet."""

    example_id: str
    span_id: str
    query: str
    gold_doc_id: str
    gold_text: str
    retrieved_top_k: list[dict[str, Any]]
    hard_negatives: list[str]
    signature: str


def _load_spans(path: Path) -> list[dict[str, Any]]:
    spans = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                spans.append(json.loads(stripped))
    return spans


def _index_spans(
    spans: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Return (workflow_by_trace, retrieval_by_trace)."""
    workflow: dict[str, dict[str, Any]] = {}
    retrieval: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        trace = span.get("trace_id", "")
        if not trace:
            continue
        if span.get("kind") == "workflow" and span.get("name") == "deep_research":
            workflow[trace] = span
        elif span.get("kind") == "retrieval" and span.get("name") == "retrieve":
            retrieval.setdefault(trace, []).append(span)
    return workflow, retrieval


def _union_retrieved(retrieval_spans: list[dict[str, Any]]) -> set[str]:
    """All doc_ids retrieved across all sub-queries for a single trace."""
    doc_ids: set[str] = set()
    for span in retrieval_spans:
        for result in span.get("attributes", {}).get("results", []):
            doc_id = result.get("doc_id")
            if doc_id:
                doc_ids.add(str(doc_id))
    return doc_ids


def _best_retrieval_span(
    retrieval_spans: list[dict[str, Any]], gold_doc_ids: set[str]
) -> dict[str, Any] | None:
    """Return the retrieval span that ranked a gold doc highest (lowest rank index).

    Falls back to the first span when no gold doc appears in any retrieved set.
    """
    best_span: dict[str, Any] | None = None
    best_rank = 999_999
    for span in retrieval_spans:
        results = span.get("attributes", {}).get("results", [])
        for rank, result in enumerate(results):
            if result.get("doc_id") in gold_doc_ids and rank < best_rank:
                best_rank = rank
                best_span = span
    return best_span if best_span is not None else (retrieval_spans[0] if retrieval_spans else None)


def mine_failures(
    examples: list[Example],
    eval_results: list[EvalResultRow],
    corpus: dict[str, str],
    *,
    spans: list[dict[str, Any]] | None = None,
    spans_path: str | PathLike[str] = "data/spans.jsonl",
    recall_scorer: str = _RECALL_SCORER,
    max_hard_negatives: int = 4,
) -> list[FailureCase]:
    """Identify retrieval failures and return one ``FailureCase`` per missed gold doc.

    Args:
        examples:          Full eval dataset (``load_dataset`` output).
        eval_results:      Per-example scorer rows from ``SqliteStore.get_eval_results``.
        corpus:            ``{doc_id: text}`` from ``load_corpus``.
        spans:             Pre-loaded spans list (skips disk read if provided).
        spans_path:        Path to ``spans.jsonl`` (used when ``spans`` is None).
        recall_scorer:     Name of the recall scorer in ``eval_results``.
        max_hard_negatives: How many retrieved-but-wrong doc_ids to attach.
    """
    # Load spans from disk only when not pre-loaded.
    if spans is None:
        spans = _load_spans(Path(spans_path))

    workflow_by_trace, retrieval_by_trace = _index_spans(spans)

    # Identify failing examples (recall < 1.0).
    failing_example_ids: set[str] = set()
    for row in eval_results:
        if row.scorer == recall_scorer and row.score < 1.0:
            failing_example_ids.add(row.example_id)

    if not failing_example_ids:
        return []

    # Build question → trace_id mapping via workflow spans.
    question_to_trace: dict[str, str] = {}
    for tid, span in workflow_by_trace.items():
        question = span.get("attributes", {}).get("question", "")
        if question:
            question_to_trace[question] = tid

    cases: list[FailureCase] = []

    for example in examples:
        if example.id not in failing_example_ids:
            continue

        question = example.input.get("question", "")
        trace_id_or_none = question_to_trace.get(question)
        if trace_id_or_none is None:
            continue
        trace_id: str = trace_id_or_none

        r_spans = retrieval_by_trace.get(trace_id, [])
        if not r_spans:
            continue

        all_gold: set[str] = {
            str(fact["doc_id"]) for fact in example.expected_output.get("supporting_facts", [])
        }
        retrieved_union = _union_retrieved(r_spans)
        missed = all_gold - retrieved_union
        if not missed:
            continue

        # One FailureCase per missed gold doc.
        for gold_doc_id in missed:
            gold_text = corpus.get(gold_doc_id, "")
            rep_span = _best_retrieval_span(r_spans, all_gold)
            if rep_span is None:
                continue

            rep_results: list[dict[str, Any]] = rep_span.get("attributes", {}).get("results", [])
            retrieved_top_k = [
                {"doc_id": r["doc_id"], "score": r.get("score", 0.0), "rank": i}
                for i, r in enumerate(rep_results, start=1)
            ]
            hard_negatives = [r["doc_id"] for r in rep_results if r.get("doc_id") not in all_gold][
                :max_hard_negatives
            ]

            rep_query: str = rep_span.get("attributes", {}).get("query") or question
            sig = classify(
                query=rep_query,
                gold_text=gold_text,
                retrieved_doc_ids=[r["doc_id"] for r in rep_results],
                all_gold_doc_ids=all_gold,
                missed_gold_doc_ids=missed,
            )

            cases.append(
                FailureCase(
                    example_id=example.id,
                    span_id=rep_span.get("span_id", ""),
                    query=rep_query,
                    gold_doc_id=gold_doc_id,
                    gold_text=gold_text,
                    retrieved_top_k=retrieved_top_k,
                    hard_negatives=hard_negatives,
                    signature=sig,
                )
            )

    return cases


_log = logging.getLogger(__name__)

_CH_HTTP_URL_ENV = "CLICKHOUSE_HTTP_URL"


async def _ch_query(http_url: str, sql: str) -> list[dict[str, Any]]:
    """Run a SQL query against ClickHouse HTTP interface, return parsed JSONEachRow."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            http_url,
            content=(sql + " FORMAT JSONEachRow").encode(),
            headers={"Content-Type": "text/plain"},
        )
        resp.raise_for_status()
    rows: list[dict[str, Any]] = []
    for line in resp.text.splitlines():
        stripped = line.strip()
        if stripped:
            rows.append(json.loads(stripped))
    return rows


async def mine_from_clickhouse(
    train_dataset: list[Any],
    eval_results: list[EvalResultRow] | None = None,
    corpus: dict[str, str] | None = None,
    *,
    ch_http_url: str,
    recall_scorer: str = _RECALL_SCORER,
    max_hard_negatives: int = 4,
) -> list[FailureCase]:
    """Mine retrieval failures directly from ClickHouse ``spans`` + ``retrievals`` tables.

    Queries ClickHouse via its HTTP interface (``CLICKHOUSE_HTTP_URL``).

    When ``eval_results`` is non-empty, only examples with recall < 1.0 are mined
    (mirrors the local ``mine_failures`` path). When absent or empty, every example
    whose gold doc is missing from the retrieved set is treated as a failure — no
    prior eval run is required (the normal production path).

    Accepts both ``Example`` objects and plain dicts with ``id``, ``input``, and
    ``expected_output`` keys (as returned by a JSONL dataset loader).

    Requires:
    - The Go collector populates the ``spans`` table with ``name='deep_research'``
      workflow spans (OTel integration active).
    - M7 retrieval fan-out populates the ``retrievals`` table.
    """
    # Query workflow spans for question → trace_id mapping.
    wf_sql = (
        "SELECT trace_id, attributes['question'] AS question"
        " FROM spans WHERE name = 'deep_research'"
    )
    wf_rows = await _ch_query(ch_http_url, wf_sql)
    question_to_trace: dict[str, str] = {}
    for row in wf_rows:
        q = row.get("question", "")
        t = row.get("trace_id", "")
        if q and t:
            question_to_trace[q] = t

    # Query retrievals table; key by trace_id.
    ret_rows = await _ch_query(
        ch_http_url,
        "SELECT trace_id, span_id, query, results FROM retrievals",
    )
    retrieval_by_trace: dict[str, list[dict[str, Any]]] = {}
    for row in ret_rows:
        # ClickHouse Array(Tuple(passage_id, score, rank)) → JSONEachRow: [[id, score, rank], ...]
        raw: list[Any] = row.get("results", [])
        results = [
            {"doc_id": str(r[0]), "score": float(r[1])}
            for r in raw
            if isinstance(r, (list, tuple)) and len(r) >= 2
        ]
        trace = row.get("trace_id", "")
        if trace:
            retrieval_by_trace.setdefault(trace, []).append(
                {
                    "trace_id": trace,
                    "span_id": row.get("span_id", ""),
                    "kind": "retrieval",
                    "name": "retrieve",
                    "attributes": {
                        "query": row.get("query", ""),
                        "results": results,
                    },
                }
            )

    if not question_to_trace:
        _log.warning("mine_from_clickhouse: no workflow spans found in ClickHouse — returning []")
        return []

    # When eval_results is non-empty, restrict to known failing examples.
    failing_ids: set[str] | None
    if eval_results:
        failing_ids = {
            r.example_id for r in eval_results if r.scorer == recall_scorer and r.score < 1.0
        }
    else:
        failing_ids = None  # mine all — gold-vs-retrieved check is the gate

    corpus_dict: dict[str, str] = corpus if corpus is not None else {}
    cases: list[FailureCase] = []

    for example in train_dataset:
        # Accept both Example objects and plain dicts.
        if isinstance(example, dict):
            example_id = str(example.get("id", ""))
            input_data: dict[str, Any] = example.get("input", {})
            expected: dict[str, Any] = example.get("expected_output", {})
        else:
            example_id = str(getattr(example, "id", ""))
            input_data = getattr(example, "input", {})
            expected = getattr(example, "expected_output", {})

        if failing_ids is not None and example_id not in failing_ids:
            continue

        question = str(input_data.get("question", ""))
        trace_id = question_to_trace.get(question)
        if not trace_id:
            continue

        r_spans = retrieval_by_trace.get(trace_id, [])
        if not r_spans:
            continue

        all_gold: set[str] = {str(f["doc_id"]) for f in expected.get("supporting_facts", [])}
        retrieved_union = _union_retrieved(r_spans)
        missed = all_gold - retrieved_union
        if not missed:
            continue

        for gold_doc_id in missed:
            gold_text = corpus_dict.get(gold_doc_id, "")
            rep_span = _best_retrieval_span(r_spans, all_gold)
            if rep_span is None:
                continue

            rep_results: list[dict[str, Any]] = rep_span.get("attributes", {}).get("results", [])
            retrieved_top_k = [
                {"doc_id": r["doc_id"], "score": r.get("score", 0.0), "rank": i}
                for i, r in enumerate(rep_results, start=1)
            ]
            hard_negatives = [r["doc_id"] for r in rep_results if r.get("doc_id") not in all_gold][
                :max_hard_negatives
            ]
            rep_query: str = rep_span.get("attributes", {}).get("query") or question
            sig = classify(
                query=rep_query,
                gold_text=gold_text,
                retrieved_doc_ids=[r["doc_id"] for r in rep_results],
                all_gold_doc_ids=all_gold,
                missed_gold_doc_ids=missed,
            )
            cases.append(
                FailureCase(
                    example_id=example_id,
                    span_id=rep_span.get("span_id", ""),
                    query=rep_query,
                    gold_doc_id=gold_doc_id,
                    gold_text=gold_text,
                    retrieved_top_k=retrieved_top_k,
                    hard_negatives=hard_negatives,
                    signature=sig,
                )
            )

    return cases


def to_store_row(case: FailureCase, *, case_id: str | None = None) -> FailureCaseRow:
    """Convert a ``FailureCase`` to the SQLite row type."""
    return FailureCaseRow(
        id=case_id or new_id(),
        example_id=case.example_id,
        span_id=case.span_id,
        query=case.query,
        gold_doc_id=case.gold_doc_id,
        retrieved_top_k=case.retrieved_top_k,
        signature=case.signature,
        mined_at=datetime.now(UTC).isoformat(),
    )
