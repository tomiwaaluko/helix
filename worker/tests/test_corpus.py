"""Corpus prep: id determinism, dedup, both context shapes, JSONL output."""

import json
from pathlib import Path

from helix.eval.corpus import build_corpus, wiki_doc_id, write_corpus

# HuggingFace shape: parallel title/sentences lists.
_HF_EXAMPLE = {
    "context": {
        "title": ["Alpha", "Beta"],
        "sentences": [["A1. ", "A2."], ["B1."]],
    }
}
# Raw shape: list of [title, [sentences]] pairs.
_RAW_EXAMPLE = {
    "context": [
        ["Beta", ["B1."]],
        ["Gamma", ["G1.", "G2."]],
    ]
}


def test_wiki_doc_id_is_deterministic_and_prefixed() -> None:
    assert wiki_doc_id("Scott Derrickson") == wiki_doc_id("Scott Derrickson")
    assert wiki_doc_id(" Scott Derrickson ") == wiki_doc_id("Scott Derrickson")  # stripped
    assert wiki_doc_id("Scott Derrickson").startswith("wiki_")
    assert wiki_doc_id("Alpha") != wiki_doc_id("Beta")


def test_build_corpus_dedups_by_title_across_examples() -> None:
    docs = build_corpus([_HF_EXAMPLE, _RAW_EXAMPLE])
    titles = [d.title for d in docs]
    assert titles == ["Alpha", "Beta", "Gamma"]  # Beta appears once, first wins

    alpha = next(d for d in docs if d.title == "Alpha")
    assert alpha.text == "A1. A2."  # sentences joined
    assert alpha.id == wiki_doc_id("Alpha")
    assert alpha.source == "hotpotqa_wiki"


def test_build_corpus_respects_limit() -> None:
    docs = build_corpus([_HF_EXAMPLE, _RAW_EXAMPLE], limit=1)
    assert {d.title for d in docs} == {"Alpha", "Beta"}  # only the first example


def test_write_corpus_roundtrips(tmp_path: Path) -> None:
    docs = build_corpus([_HF_EXAMPLE])
    path = tmp_path / "corpus.jsonl"
    count = write_corpus(docs, path)

    assert count == 2
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {r["title"] for r in rows} == {"Alpha", "Beta"}
    assert set(rows[0]) == {"id", "text", "source", "title"}
