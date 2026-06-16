#!/usr/bin/env python
"""Format a HotpotQA mining/train split for the embedding fine-tune loop.

Writes ``evals/datasets/hotpotqa_train_1000.jsonl`` — questions 600-1600 of the
distractor-validation set. This split is **disjoint** from both the dev set
(questions 0-100) and the sequestered holdout (questions 100-600), so failures
mined here can train the embedding model while ``hotpotqa_dev_100`` stays a fair
held-out measurement set for recall@10 lift (and the holdout stays sequestered
for ``make eval-final``). See ``docs/vertical-slice-plan.md`` and the failure-loop
plan.

Formatting + integrity logic lives in ``helix.eval`` and is unit tested; this
file owns only the dataset download and file output.

Run ``python scripts/prepare_corpus.py`` first so the corpus covers this split's
supporting paragraphs, then ``python scripts/prepare_train_split.py``.
"""

from __future__ import annotations

from pathlib import Path

from helix.eval.corpus import load_corpus_ids
from helix.eval.hotpotqa import build_questions, missing_doc_ids, write_examples

TRAIN_PATH = "evals/datasets/hotpotqa_train_1000.jsonl"
CORPUS_PATH = "data/corpus.jsonl"
# Questions [TRAIN_START, TRAIN_START + TRAIN_COUNT) — after dev (0-100) and the
# holdout (100-600). prepare_corpus.py must cover at least this far (it builds
# from the first 1600 questions); under-covering silently breaks referential
# integrity (the train split's supporting paragraphs would be absent).
TRAIN_START = 600
TRAIN_COUNT = 1000


def _validate(questions: list[dict[str, object]]) -> None:
    corpus = Path(CORPUS_PATH)
    if not corpus.exists():
        print(f"Skipped integrity check ({CORPUS_PATH} not found)")
        return
    missing = missing_doc_ids(questions, load_corpus_ids(corpus))
    if missing:
        raise SystemExit(
            f"Referential integrity failed for train split: {len(missing)} supporting "
            f"doc_ids not in {CORPUS_PATH} (run prepare_corpus.py first)."
        )
    print("Referential integrity OK for train split")


def main() -> None:
    from datasets import load_dataset

    # See prepare_corpus.py: the dataset moved to `hotpotqa/hotpot_qa` (Parquet).
    dataset = load_dataset("hotpotqa/hotpot_qa", "distractor", split="validation")

    train = build_questions(
        dataset, start=TRAIN_START, count=TRAIN_COUNT, id_prefix="hotpotqa_train"
    )
    _validate(train)
    written = write_examples(train, TRAIN_PATH)
    print(f"Wrote {written} questions to {TRAIN_PATH}")


if __name__ == "__main__":
    main()
