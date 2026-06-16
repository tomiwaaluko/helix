"""Disk-backed LLM response cache.

Makes eval reruns free and deterministic: the first run pays for the LiteLLM
calls, every rerun with identical generation parameters replays from disk. Each
entry is a JSON file under ``data/llm_cache/`` named by its cache key.

The key covers every parameter that affects output — ``model``, ``messages``,
``temperature``, ``max_tokens``, ``top_p``, ``stop`` — canonicalized with sorted
keys and tight separators. ``None`` values are dropped before hashing so adding
an explicit ``top_p=None`` does not change the key. No TTL, no eviction; delete
``data/llm_cache/`` to clear.
"""

from __future__ import annotations

import hashlib
import json
import os
from os import PathLike, fspath
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path("data/llm_cache")


def cache_enabled() -> bool:
    """On by default outside production; ``HELIX_LLM_CACHE=0`` (``--no-cache``) disables it."""
    if os.environ.get("HELIX_LLM_CACHE") == "0":
        return False
    return os.environ.get("HELIX_ENV", "dev") != "production"


class LLMCache:
    """JSON-file cache of LLM responses, keyed by generation parameters."""

    def __init__(self, directory: str | PathLike[str] = DEFAULT_CACHE_DIR) -> None:
        self._dir = Path(fspath(directory))

    def key(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int | None,
        top_p: float | None,
        stop: list[str] | str | None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
            "stop": stop,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._dir / f"{key}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{key}.json").write_text(json.dumps(value), encoding="utf-8")
