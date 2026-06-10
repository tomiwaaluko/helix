"""Document chunking for the indexer.

Structural splitting: paragraphs first (blank-line boundaries), then sentences
for any paragraph over the size budget, then a word-window fallback for a
sentence that is still too long. Segments are greedily packed into chunks up to
``max_tokens`` with ``overlap_tokens`` carried from the tail of one chunk into
the next so retrieval doesn't lose context that straddles a boundary.

Token counting uses ``tiktoken`` (``cl100k_base``) — good enough for sizing and
not tied to a specific model. The counter is injectable so tests run without the
tokenizer.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH_BOUNDARY = re.compile(r"\n\s*\n")

TokenCounter = Callable[[str], int]


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    source: str
    token_count: int


def _tiktoken_counter() -> TokenCounter:
    import tiktoken

    encoding = tiktoken.get_encoding("cl100k_base")

    def count(text: str) -> int:
        return len(encoding.encode(text))

    return count


def _split_words(text: str, max_tokens: int, count: TokenCounter) -> list[str]:
    """Last resort: pack words into pieces that fit ``max_tokens``."""
    pieces: list[str] = []
    current: list[str] = []
    for word in text.split():
        if current and count(" ".join([*current, word])) > max_tokens:
            pieces.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _segment(text: str, max_tokens: int, count: TokenCounter) -> list[str]:
    """Break text into segments that each fit within ``max_tokens``."""
    segments: list[str] = []
    for paragraph in (p.strip() for p in _PARAGRAPH_BOUNDARY.split(text)):
        if not paragraph:
            continue
        if count(paragraph) <= max_tokens:
            segments.append(paragraph)
            continue
        for sentence in (s.strip() for s in _SENTENCE_BOUNDARY.split(paragraph)):
            if not sentence:
                continue
            if count(sentence) <= max_tokens:
                segments.append(sentence)
            else:
                segments.extend(_split_words(sentence, max_tokens, count))
    return segments


def _overlap_tail(
    segments: list[str], overlap_tokens: int, budget: int, count: TokenCounter
) -> list[str]:
    """Trailing segments of a chunk to carry into the next, within both limits."""
    limit = min(overlap_tokens, max(budget, 0))
    tail: list[str] = []
    total = 0
    for segment in reversed(segments):
        tokens = count(segment)
        if total + tokens > limit:
            break
        tail.insert(0, segment)
        total += tokens
    return tail


def chunk_document(
    doc_id: str,
    text: str,
    source: str,
    *,
    max_tokens: int = 512,
    overlap_tokens: int = 64,
    token_counter: TokenCounter | None = None,
) -> list[Chunk]:
    """Split a document into overlapping, size-bounded chunks.

    Chunk ids are deterministic: ``{doc_id}#chunk{N}``.
    """
    count = token_counter or _tiktoken_counter()
    segments = _segment(text, max_tokens, count)

    chunks: list[Chunk] = []
    current: list[str] = []

    def flush() -> None:
        if not current:
            return
        chunk_text = " ".join(current)
        chunks.append(
            Chunk(
                chunk_id=f"{doc_id}#chunk{len(chunks)}",
                doc_id=doc_id,
                text=chunk_text,
                source=source,
                token_count=count(chunk_text),
            )
        )

    for segment in segments:
        if current and count(" ".join(current)) + count(segment) > max_tokens:
            previous = current
            flush()
            current = _overlap_tail(previous, overlap_tokens, max_tokens - count(segment), count)
            current.append(segment)
        else:
            current.append(segment)
    flush()
    return chunks
