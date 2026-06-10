#!/usr/bin/env python
"""Format HotpotQA distractor-dev into the Helix eval dataset.

Thin wrapper: formatting + integrity logic lives in ``helix.eval.hotpotqa`` (and
is unit-tested there). This file owns the dataset download and file output, and
validates referential integrity against ``data/corpus.jsonl`` if it exists.

Run: ``python scripts/prepare_hotpotqa.py``
"""

from __future__ import annotations

from pathlib import Path

from helix.eval.corpus import load_corpus_ids
from helix.eval.hotpotqa import build_questions, missing_doc_ids, write_examples

DATASET_PATH = "evals/datasets/hotpotqa_dev_100.jsonl"
CORPUS_PATH = "data/corpus.jsonl"
NUM_QUESTIONS = 100


def main() -> None:
    from datasets import load_dataset

    dataset = load_dataset("hotpot_qa", "distractor", split="validation")
    questions = build_questions(dataset, start=0, count=NUM_QUESTIONS)
    written = write_examples(questions, DATASET_PATH)

    corpus = Path(CORPUS_PATH)
    if corpus.exists():
        missing = missing_doc_ids(questions, load_corpus_ids(corpus))
        if missing:
            raise SystemExit(
                f"Referential integrity failed: {len(missing)} supporting doc_ids "
                f"not in {CORPUS_PATH} (run prepare_corpus.py first)."
            )
        print(f"Referential integrity OK against {CORPUS_PATH}")
    else:
        print(f"Skipped integrity check ({CORPUS_PATH} not found)")

    print(f"Wrote {written} questions to {DATASET_PATH}")


if __name__ == "__main__":
    main()
