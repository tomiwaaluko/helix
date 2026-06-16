#!/usr/bin/env python
"""Verify the holdout dataset matches its committed SHA-256 hash-lock.

Run as part of ``make lint``. Skips cleanly (exit 0) if the holdout dataset has
not been generated yet, so lint stays green on a fresh checkout before seeding.

This script is explicitly allowed to reference the holdout filename (see
.github/workflows/holdout-guard.yml) because its sole purpose is integrity
verification, not data access.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

HOLDOUT_DATASET_PATH = "evals/datasets/hotpotqa_dev_holdout_500.jsonl"
HOLDOUT_SHA256_PATH = "evals/datasets/hotpotqa_dev_holdout_500.sha256"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    dataset = Path(HOLDOUT_DATASET_PATH)
    lock = Path(HOLDOUT_SHA256_PATH)
    if not dataset.exists() or not lock.exists():
        print("Holdout dataset not present; skipping integrity check.")
        return 0

    expected = lock.read_text(encoding="utf-8").strip()
    actual = sha256_file(dataset)
    if actual != expected:
        print(
            f"Holdout integrity FAILED: {HOLDOUT_DATASET_PATH}\n"
            f"  expected {expected}\n  actual   {actual}",
            file=sys.stderr,
        )
        return 1
    print(f"Holdout integrity OK ({actual[:12]}…)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
