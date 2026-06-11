#!/usr/bin/env python
"""Format HotpotQA distractor-dev into the Helix eval datasets.

Writes the 100-question dev set and the sequestered 500-question holdout set
(disjoint: dev = questions 1-100, holdout = 101-600), plus the holdout's SHA-256
hash-lock. Formatting + integrity logic lives in ``helix.eval`` and is unit
tested; this file owns the dataset download and file output.

The holdout path literals are imported from ``helix.eval.harness`` so this file
and the harness are the only places the holdout filename appears (the CI guard
whitelists exactly those two files).

Run: ``python scripts/prepare_hotpotqa.py``
"""

from __future__ import annotations

from pathlib import Path

from helix.eval.corpus import load_corpus_ids
from helix.eval.harness import HOLDOUT_DATASET_PATH, HOLDOUT_SHA256_PATH, sha256_file
from helix.eval.hotpotqa import build_questions, missing_doc_ids, write_examples

DEV_PATH = "evals/datasets/hotpotqa_dev_100.jsonl"
CORPUS_PATH = "data/corpus.jsonl"
DEV_COUNT = 100
HOLDOUT_COUNT = 500


def _validate(questions: list[dict[str, object]], label: str) -> None:
    corpus = Path(CORPUS_PATH)
    if not corpus.exists():
        print(f"Skipped integrity check for {label} ({CORPUS_PATH} not found)")
        return
    missing = missing_doc_ids(questions, load_corpus_ids(corpus))
    if missing:
        raise SystemExit(
            f"Referential integrity failed for {label}: {len(missing)} supporting "
            f"doc_ids not in {CORPUS_PATH} (run prepare_corpus.py first)."
        )
    print(f"Referential integrity OK for {label}")


def main() -> None:
    from datasets import load_dataset

    # See prepare_corpus.py: the dataset moved to `hotpotqa/hotpot_qa` (Parquet).
    # build_questions handles the Parquet supporting_facts shape.
    dataset = load_dataset("hotpotqa/hotpot_qa", "distractor", split="validation")

    dev = build_questions(dataset, start=0, count=DEV_COUNT)
    _validate(dev, "dev")
    written = write_examples(dev, DEV_PATH)
    print(f"Wrote {written} questions to {DEV_PATH}")

    holdout = build_questions(
        dataset, start=DEV_COUNT, count=HOLDOUT_COUNT, id_prefix="hotpotqa_holdout"
    )
    _validate(holdout, "holdout")
    written = write_examples(holdout, HOLDOUT_DATASET_PATH)
    digest = sha256_file(HOLDOUT_DATASET_PATH)
    Path(HOLDOUT_SHA256_PATH).write_text(digest + "\n", encoding="utf-8")
    print(f"Wrote {written} questions to {HOLDOUT_DATASET_PATH} (sha256 {digest[:12]}…)")


if __name__ == "__main__":
    main()
