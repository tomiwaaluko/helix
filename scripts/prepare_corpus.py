#!/usr/bin/env python
"""Download HotpotQA distractor-dev wiki paragraphs into ``data/corpus.jsonl``.

Thin wrapper: the formatting/dedup/id logic lives in ``helix.eval.corpus`` (and
is unit-tested there). This file owns only the dataset download and file output.

Run: ``python scripts/prepare_corpus.py``
"""

from __future__ import annotations

from helix.eval.corpus import build_corpus, write_corpus

CORPUS_PATH = "data/corpus.jsonl"
# The corpus must cover every question across all three splits:
#   dev      = questions 0-100      (prepare_hotpotqa.py)
#   holdout  = questions 100-600    (prepare_hotpotqa.py, sequestered)
#   train    = questions 600-1600   (prepare_train_split.py, fine-tune mining)
# So index the union: the first 1600 questions. (Under-covering silently breaks
# referential integrity — a split's supporting paragraphs would be absent.)
NUM_QUESTIONS = 1600


def main() -> None:
    from datasets import load_dataset

    # The dataset moved to the `hotpotqa/` namespace (and to Parquet); the bare
    # `hotpot_qa` id no longer resolves on the Hub. build_corpus handles the
    # Parquet context shape.
    dataset = load_dataset("hotpotqa/hotpot_qa", "distractor", split="validation")
    docs = build_corpus(dataset, limit=NUM_QUESTIONS)
    count = write_corpus(docs, CORPUS_PATH)
    print(f"Wrote {count} documents to {CORPUS_PATH}")


if __name__ == "__main__":
    main()
