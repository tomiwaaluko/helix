"""The reference multi-hop research agent, as a Helix workflow.

DAG: ``decompose(question)`` → parallel ``retrieve(subquery)`` → ``synthesize``.
``decompose`` and ``synthesize`` are LLM calls (``temperature=0``); ``retrieve``
runs the hybrid retriever. The workflow attaches the deduped union of retrieved
``doc_id``s to ``Answer.metadata["retrieved_doc_ids"]`` — without it the recall
scorer silently reads zero.

Dependencies (retriever, model, span logger, LLM injection points) are supplied
via a ``contextvars`` ``ResearchDeps`` set by ``using_research_deps(...)``. This
works in ``.local()`` mode; submit-mode dispatch would need the deps threaded
through the engine.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from helix import gather, task, workflow
from helix.logging import SpanLogger
from helix.rag.retriever import HybridRetriever
from helix.tools.litellm_adapter import llm_call
from helix.tools.llm_cache import LLMCache
from helix.types import Answer, Citation, Doc

_PROMPT_DIR = Path(__file__).parent / "prompts"
DECOMPOSE_PROMPT = (_PROMPT_DIR / "decompose.txt").read_text(encoding="utf-8")
SYNTHESIZE_PROMPT = (_PROMPT_DIR / "synthesize.txt").read_text(encoding="utf-8")

_DEFAULT_LOGGER = SpanLogger()
_CITATIONS_MARKER = "CITATIONS:"


@dataclass
class ResearchDeps:
    """Runtime dependencies for a deep_research run."""

    retriever: HybridRetriever
    top_k: int = 10
    model: str | None = None
    span_logger: SpanLogger | None = None
    cache: LLMCache | None = None
    completion_fn: Callable[..., Awaitable[Any]] | None = None
    cost_fn: Callable[[Any], float] | None = None


_deps: ContextVar[ResearchDeps | None] = ContextVar("helix_research_deps", default=None)


@contextmanager
def using_research_deps(deps: ResearchDeps) -> Iterator[ResearchDeps]:
    """Bind research dependencies for the duration of a deep_research run."""
    token = _deps.set(deps)
    try:
        yield deps
    finally:
        _deps.reset(token)


def _require_deps() -> ResearchDeps:
    deps = _deps.get()
    if deps is None:
        raise RuntimeError(
            "deep_research requires research deps; wrap the call in using_research_deps(...)"
        )
    return deps


def _extract_json_array(text: str) -> list[Any] | None:
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return parsed
    match = re.search(r"\[.*\]", stripped, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, list):
            return parsed
    return None


def _parse_subqueries(text: str, fallback: str, *, max_subqueries: int = 4) -> list[str]:
    array = _extract_json_array(text)
    if array is not None:
        subqueries = [str(item).strip() for item in array if str(item).strip()]
        if subqueries:
            return subqueries[:max_subqueries]
    return [fallback]


def _format_evidence(evidence: list[Doc]) -> str:
    return "\n\n".join(f"[doc_id={doc.id}] {doc.text}" for doc in evidence)


def _parse_synthesis(text: str, evidence: list[Doc]) -> Answer:
    answer_text = text.strip()
    cited_ids: list[str] = []
    marker_at = text.rfind(_CITATIONS_MARKER)
    if marker_at != -1:
        answer_text = text[:marker_at].strip()
        raw = text[marker_at + len(_CITATIONS_MARKER) :]
        cited_ids = [token.strip() for token in raw.split(",") if token.strip()]

    by_id = {doc.id: doc for doc in evidence}
    citations: list[Citation] = []
    seen: set[str] = set()
    for doc_id in cited_ids:
        doc = by_id.get(doc_id)
        if doc is not None and doc_id not in seen:
            seen.add(doc_id)
            citations.append(Citation(doc_id=doc_id, passage=doc.text, relevance=doc.score))
    return Answer(text=answer_text, citations=citations, metadata={})


@task(retries=1, timeout="30s")
async def decompose(question: str) -> list[str]:
    deps = _require_deps()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": DECOMPOSE_PROMPT},
        {"role": "user", "content": question},
    ]
    response = await llm_call(
        messages,
        model=deps.model,
        span_logger=deps.span_logger,
        cache=deps.cache,
        completion_fn=deps.completion_fn,
        cost_fn=deps.cost_fn,
    )
    return _parse_subqueries(response.text, fallback=question)


@task(retries=2, timeout="2m")
async def retrieve(subquery: str) -> list[Doc]:
    deps = _require_deps()
    return await deps.retriever.retrieve(subquery, top_k=deps.top_k)


@task(retries=1, timeout="3m")
async def synthesize(question: str, evidence: list[Doc]) -> Answer:
    deps = _require_deps()
    user = f"Question: {question}\n\nEvidence:\n{_format_evidence(evidence)}"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYNTHESIZE_PROMPT},
        {"role": "user", "content": user},
    ]
    response = await llm_call(
        messages,
        model=deps.model,
        span_logger=deps.span_logger,
        cache=deps.cache,
        completion_fn=deps.completion_fn,
        cost_fn=deps.cost_fn,
    )
    return _parse_synthesis(response.text, evidence)


@workflow(name="deep_research", version="1.0.0")
async def deep_research(question: str) -> Answer:
    deps = _require_deps()
    logger = deps.span_logger or _DEFAULT_LOGGER
    with logger.span("deep_research", kind="workflow", attributes={"question": question}):
        subqueries = await decompose(question)
        evidence_per_subquery = await gather(*(retrieve(sq) for sq in subqueries))

        # Union of retrieved doc_ids across all subqueries, deduped, order preserved.
        retrieved_ids: list[str] = []
        seen: set[str] = set()
        for docs in evidence_per_subquery:
            for doc in docs:
                if doc.id not in seen:
                    seen.add(doc.id)
                    retrieved_ids.append(doc.id)

        flat = [doc for docs in evidence_per_subquery for doc in docs]
        answer = await synthesize(question, flat)
        answer.metadata["retrieved_doc_ids"] = retrieved_ids
    return answer
