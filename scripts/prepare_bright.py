"""Prepare the BRIGHT biology retrieval benchmark for use with the Helix slice.

Downloads the xlangai/BRIGHT dataset from HuggingFace and produces:
  data/bright_corpus.jsonl          — pre-chunked biology docs (gold + sampled distractors)
  evals/datasets/bright_biology_dev.jsonl — all 103 biology queries in Helix eval format

Because BRIGHT documents are already short passages (~100–500 words), we skip
the Helix chunker and mark each document as a single-chunk corpus entry. The
indexer will produce exactly one chunk per document (id = doc_id).

Gold document IDs in supporting_facts use the sanitized BRIGHT ID so the recall
scorer can compare directly against retrieved Doc.id values.

Usage:
    python3.12 scripts/prepare_bright.py [--max-distractors N]

    --max-distractors  Maximum number of non-gold distractor docs to include
                       (default 10000). All gold docs are always included.
                       Use 0 for the full corpus (~57k docs; takes ~45 min to index).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
CORPUS_PATH = ROOT / "data" / "bright_corpus.jsonl"
DEV_PATH = ROOT / "evals" / "datasets" / "bright_biology_dev.jsonl"
DOMAIN = "biology"
SEED = 42


def sanitize_id(bright_id: str) -> str:
    """Remove .txt suffix; keep path separators as-is (valid in our string IDs)."""
    return bright_id.removesuffix(".txt")


def main(max_distractors: int) -> None:
    try:
        from datasets import load_dataset  # type: ignore[import-untyped]
    except ImportError:
        print("ERROR: pip install datasets", file=sys.stderr)
        sys.exit(1)

    print(f"Downloading BRIGHT '{DOMAIN}' documents …")
    docs_ds = load_dataset("xlangai/BRIGHT", "documents", split=DOMAIN)

    print(f"Downloading BRIGHT '{DOMAIN}' examples …")
    ex_ds = load_dataset("xlangai/BRIGHT", "examples", split=DOMAIN)

    # Collect gold doc IDs (sanitized) across all queries
    gold_ids: set[str] = set()
    for ex in ex_ds:
        for gid in ex["gold_ids"]:
            gold_ids.add(sanitize_id(gid))

    print(f"  {len(ex_ds)} queries, {len(gold_ids)} unique gold docs in corpus of {len(docs_ds)}")

    # Partition: gold docs always included; sample remaining distractors
    gold_docs: list[dict[str, str]] = []
    distractor_docs: list[dict[str, str]] = []
    for row in docs_ds:
        sid = sanitize_id(row["id"])
        entry = {"id": sid, "text": row["content"], "source": f"bright_{DOMAIN}"}
        if sid in gold_ids:
            gold_docs.append(entry)
        else:
            distractor_docs.append(entry)

    if max_distractors > 0 and len(distractor_docs) > max_distractors:
        rng = random.Random(SEED)
        distractor_docs = rng.sample(distractor_docs, max_distractors)
        print(f"  Sampled {len(distractor_docs)} distractors (full pool: {len(docs_ds) - len(gold_ids)})")
    else:
        print(f"  Using all {len(distractor_docs)} distractors (full corpus mode)")

    corpus = gold_docs + distractor_docs
    print(f"  Corpus total: {len(corpus)} docs ({len(gold_docs)} gold + {len(distractor_docs)} distractor)")

    CORPUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CORPUS_PATH.open("w") as f:
        for doc in corpus:
            f.write(json.dumps(doc) + "\n")
    print(f"Wrote {CORPUS_PATH}")

    # Build eval dataset
    DEV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DEV_PATH.open("w") as f:
        for i, ex in enumerate(ex_ds):
            gold = [sanitize_id(gid) for gid in ex["gold_ids"]]
            # Only include gold docs that are actually in our corpus
            gold_in_corpus = [g for g in gold if g in gold_ids]
            supporting_facts = [{"doc_id": g, "sent": 0} for g in gold_in_corpus]
            record = {
                "id": f"bright_biology_{i:03d}",
                "input": {"question": ex["query"]},
                "expected_output": {
                    "answer": ex.get("gold_answer", ""),
                    "supporting_facts": supporting_facts,
                },
                "metadata": {
                    "domain": DOMAIN,
                    "bright_id": ex["id"],
                    "num_gold": len(gold_in_corpus),
                },
            }
            f.write(json.dumps(record) + "\n")
    print(f"Wrote {DEV_PATH} ({len(ex_ds)} examples)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-distractors",
        type=int,
        default=10_000,
        help="Max distractor docs (0 = full corpus). Default: 10000",
    )
    args = parser.parse_args()
    main(args.max_distractors)
