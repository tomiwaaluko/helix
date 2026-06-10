"""Core data types shared across the SDK, RAG, and eval surfaces.

These are plain dataclasses on purpose: they cross the workflow/eval boundary
and must be trivial to construct in tests and serialize into spans.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Doc:
    """A retrieved document (or chunk) with an optional relevance score."""

    id: str
    text: str
    source: str
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Citation:
    """A passage cited in an answer, traceable back to a source document."""

    doc_id: str
    passage: str
    relevance: float = 0.0


@dataclass
class Answer:
    """A workflow's final answer plus the citations that support it.

    Note: workflows must populate ``metadata["retrieved_doc_ids"]`` with the
    union of retrieved doc IDs, or ``retrieval_recall@10`` scores silently zero.
    """

    text: str
    citations: list[Citation]
    metadata: dict[str, Any] = field(default_factory=dict)
