#!/usr/bin/env python
"""Download HotpotQA distractor-dev wiki paragraphs into ``data/corpus.jsonl``.

Thin wrapper: the formatting/dedup/id logic lives in ``helix.eval.corpus`` (and
is unit-tested there). This file owns only the dataset download and file output.

Run: ``python scripts/prepare_corpus.py``
"""

from __future__ import annotations

from helix.eval.corpus import build_corpus, write_corpus

CORPUS_PATH = "data/corpus.jsonl"
# Unique paragraphs from the first 500 distractor-dev questions reach ~1–2k docs;
# we only evaluate on the first 100 (see prepare_hotpotqa.py).
NUM_QUESTIONS = 500


def main() -> None:
    from datasets import load_dataset

    dataset = load_dataset("hotpot_qa", "distractor", split="validation")
    docs = build_corpus(dataset, limit=NUM_QUESTIONS)
    count = write_corpus(docs, CORPUS_PATH)
    print(f"Wrote {count} documents to {CORPUS_PATH}")


if __name__ == "__main__":
    main()
