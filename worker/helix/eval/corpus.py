"""Corpus preparation logic shared by the seed scripts.

The ``doc_id`` scheme (``wiki_<title_hash>``) lives here so both the corpus
script (Task 13) and the question script (Task 14) derive identical ids for the
same Wikipedia title — that referential integrity is what lets the recall scorer
match retrieved docs against gold ``supporting_facts``.

Network/dataset loading stays in the thin ``scripts/`` wrappers; everything here
is pure and unit-tested.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from os import PathLike
from pathlib import Path
from typing import Any

SOURCE = "hotpotqa_wiki"


def wiki_doc_id(title: str) -> str:
    """Deterministic corpus id for a Wikipedia title."""
    digest = hashlib.sha1(title.strip().encode("utf-8")).hexdigest()[:16]  # noqa: S324
    return f"wiki_{digest}"


@dataclass(frozen=True)
class CorpusDoc:
    id: str
    text: str
    source: str
    title: str


def _iter_paragraphs(context: Any) -> Iterator[tuple[str, list[str]]]:
    """Yield (title, sentences) from either HotpotQA context shape.

    The HuggingFace ``hotpot_qa`` build stores context as parallel
    ``{"title": [...], "sentences": [[...], ...]}`` lists; the raw dataset uses a
    list of ``[title, [sentences]]`` pairs. Handle both.
    """
    if isinstance(context, Mapping):
        titles = context.get("title", [])
        sentences = context.get("sentences", [])
        for title, sents in zip(titles, sentences, strict=False):
            yield str(title), [str(s) for s in sents]
    else:
        for item in context:
            yield str(item[0]), [str(s) for s in item[1]]


def build_corpus(
    examples: Iterable[Mapping[str, Any]], *, limit: int | None = None
) -> list[CorpusDoc]:
    """Collect unique Wikipedia paragraphs from HotpotQA examples, deduped by title."""
    by_title: dict[str, CorpusDoc] = {}
    for index, example in enumerate(examples):
        if limit is not None and index >= limit:
            break
        for title, sentences in _iter_paragraphs(example.get("context")):
            if title in by_title:
                continue
            by_title[title] = CorpusDoc(
                id=wiki_doc_id(title),
                text="".join(sentences),
                source=SOURCE,
                title=title,
            )
    return list(by_title.values())


def write_corpus(docs: Iterable[CorpusDoc], path: str | PathLike[str]) -> int:
    """Write docs as JSONL; returns the number written."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with destination.open("w", encoding="utf-8") as handle:
        for doc in docs:
            handle.write(json.dumps(asdict(doc)) + "\n")
            count += 1
    return count
