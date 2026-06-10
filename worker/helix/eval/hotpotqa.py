"""HotpotQA question formatting for the eval dataset.

Turns raw HotpotQA distractor examples into the Helix eval schema
(``data-model.md``). Supporting-fact titles are hashed with ``wiki_doc_id`` —
the same function the corpus uses — so ``supporting_facts[].doc_id`` resolves
against the corpus. Pure and unit-tested; the dataset download lives in
``scripts/prepare_hotpotqa.py``.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterable, Iterator, Mapping
from os import PathLike
from pathlib import Path
from typing import Any

from helix.eval.corpus import wiki_doc_id


def _iter_supporting(supporting_facts: Any) -> Iterator[tuple[str, int]]:
    """Yield (title, sentence_index) from either HotpotQA supporting-fact shape."""
    if isinstance(supporting_facts, Mapping):
        titles = supporting_facts.get("title", [])
        sent_ids = supporting_facts.get("sent_id", [])
        for title, sent in zip(titles, sent_ids, strict=False):
            yield str(title), int(sent)
    else:
        for item in supporting_facts:
            yield str(item[0]), int(item[1])


def build_questions(
    examples: Iterable[Mapping[str, Any]],
    *,
    start: int = 0,
    count: int = 100,
    id_prefix: str = "hotpotqa_dev",
    id_start: int = 1,
) -> list[dict[str, Any]]:
    """Format ``count`` examples (from offset ``start``) as Helix eval rows."""
    questions: list[dict[str, Any]] = []
    window = itertools.islice(examples, start, start + count)
    for offset, example in enumerate(window):
        supporting = [
            {"doc_id": wiki_doc_id(title), "sent": sent}
            for title, sent in _iter_supporting(example.get("supporting_facts"))
        ]
        questions.append(
            {
                "id": f"{id_prefix}_{id_start + offset:03d}",
                "input": {"question": str(example.get("question", ""))},
                "expected_output": {
                    "answer": str(example.get("answer", "")),
                    "supporting_facts": supporting,
                },
                "metadata": {
                    "hops": len({s["doc_id"] for s in supporting}),
                    "type": str(example.get("type", "")),
                },
            }
        )
    return questions


def write_examples(examples: Iterable[Mapping[str, Any]], path: str | PathLike[str]) -> int:
    """Write eval rows as JSONL; returns the number written."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with destination.open("w", encoding="utf-8") as handle:
        for example in examples:
            handle.write(json.dumps(example) + "\n")
            count += 1
    return count


def supporting_doc_ids(questions: Iterable[Mapping[str, Any]]) -> set[str]:
    """All ``doc_id``s referenced by the questions' supporting facts."""
    return {
        str(fact["doc_id"])
        for question in questions
        for fact in question["expected_output"]["supporting_facts"]
    }


def missing_doc_ids(questions: Iterable[Mapping[str, Any]], corpus_ids: set[str]) -> set[str]:
    """Supporting-fact ``doc_id``s not present in the corpus (empty == integrity holds)."""
    return supporting_doc_ids(questions) - corpus_ids
