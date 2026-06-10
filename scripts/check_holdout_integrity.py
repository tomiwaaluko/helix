#!/usr/bin/env python
"""Verify the holdout dataset matches its committed SHA-256 hash-lock.

Run as part of ``make lint``. Skips cleanly (exit 0) if the holdout dataset has
not been generated yet, so lint stays green on a fresh checkout before seeding.

The holdout path constants are imported from ``helix.eval.harness`` so this file
does not contain the holdout filename literal (the CI guard would otherwise flag
it).
"""

from __future__ import annotations

import sys
from pathlib import Path

from helix.eval.harness import HOLDOUT_DATASET_PATH, HOLDOUT_SHA256_PATH, sha256_file


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
