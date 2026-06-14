"""LiteLLM tool adapter.

A thin async wrapper around LiteLLM's ``acompletion`` that emits a ``kind="llm"``
span and goes through the disk-backed cache. The adapter never implements its own
retry loop — it asks LiteLLM to own provider retries via ``num_retries`` (LiteLLM
retries retryable errors such as 503/429/timeouts with exponential backoff). It
never talks to a provider directly — all model calls go through LiteLLM so the
cache, metering, and (future) replay stay correct.

The LiteLLM call and cost functions are lazily imported and injectable, so the
adapter is unit-testable without the dependency installed or a network/API key.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from helix.logging import SpanLogger, new_id
from helix.tools.blob import BlobStore, maybe_offload
from helix.tools.llm_cache import LLMCache, cache_enabled
from helix.tools.rate_limit import RedisRateLimiter

_DEFAULT_MODEL = "claude-sonnet-4-20250514"
_DEFAULT_NUM_RETRIES = 4
_DEFAULT_LOGGER = SpanLogger()


def _span_payloads_enabled() -> bool:
    """Whether to capture prompt/completion payloads on LLM spans (``HELIX_SPAN_PAYLOADS``).

    Off by default: payloads stay out of spans, preserving privacy posture and eval
    determinism. When on, large payloads are offloaded to MinIO by reference.
    """
    return os.environ.get("HELIX_SPAN_PAYLOADS", "").lower() in ("1", "true", "yes")


def _default_num_retries() -> int:
    """LiteLLM retry budget for transient provider errors (``HELIX_LLM_NUM_RETRIES``)."""
    raw = os.environ.get("HELIX_LLM_NUM_RETRIES")
    if raw is None:
        return _DEFAULT_NUM_RETRIES
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_NUM_RETRIES


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    cache_hit: bool = False
    replayed: bool = False


def _lazy_acompletion() -> Callable[..., Awaitable[Any]]:
    import litellm

    return cast("Callable[..., Awaitable[Any]]", litellm.acompletion)


def _lazy_cost() -> Callable[[Any], float]:
    import litellm

    return cast("Callable[[Any], float]", litellm.completion_cost)


def _extract(raw: Any) -> tuple[str, int, int]:
    content = raw.choices[0].message.content
    text = content if isinstance(content, str) else ""
    usage = raw.usage
    return text, int(usage.prompt_tokens), int(usage.completion_tokens)


async def llm_call(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    top_p: float | None = None,
    stop: list[str] | str | None = None,
    num_retries: int | None = None,
    cache: LLMCache | None = None,
    span_logger: SpanLogger | None = None,
    completion_fn: Callable[..., Awaitable[Any]] | None = None,
    cost_fn: Callable[[Any], float] | None = None,
    rate_limiter: RedisRateLimiter | None = None,
    blob_store: BlobStore | None = None,
) -> LLMResponse:
    """Call a model via LiteLLM (cached), emitting a ``kind="llm"`` span.

    ``temperature`` defaults to 0 for deterministic eval/research output. On a
    cache hit the returned ``cost_usd`` is 0 (so summing across reruns does not
    over-count) and ``cache_hit``/``replayed`` are ``True``; token counts are
    replayed from the cached response for information only.
    """
    model = model or os.environ.get("HELIX_DEFAULT_MODEL", _DEFAULT_MODEL)
    cache = cache or LLMCache()
    logger = span_logger or _DEFAULT_LOGGER

    use_cache = cache_enabled()
    key = cache.key(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=top_p,
        stop=stop,
    )
    cached = cache.get(key) if use_cache else None

    with logger.span("llm_call", kind="llm") as attrs:
        attrs["model"] = model
        if cached is not None:
            resp = LLMResponse(
                text=str(cached["text"]),
                model=str(cached["model"]),
                prompt_tokens=int(cached["prompt_tokens"]),
                completion_tokens=int(cached["completion_tokens"]),
                cost_usd=0.0,
                cache_hit=True,
                replayed=True,
            )
        else:
            acompletion = completion_fn or _lazy_acompletion()
            cost = cost_fn or _lazy_cost()
            retries = num_retries if num_retries is not None else _default_num_retries()
            # Distributed rate gate (no-op without Redis); skipped on cache hits above.
            if rate_limiter is not None:
                await rate_limiter.acquire()
            raw = await acompletion(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                stop=stop,
                num_retries=retries,
            )
            text, prompt_tokens, completion_tokens = _extract(raw)
            resp = LLMResponse(
                text=text,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=float(cost(raw)),
            )
            if use_cache:
                cache.put(
                    key,
                    {
                        "text": resp.text,
                        "model": resp.model,
                        "prompt_tokens": resp.prompt_tokens,
                        "completion_tokens": resp.completion_tokens,
                        "cost_usd": resp.cost_usd,
                    },
                )

        attrs["prompt_tokens"] = resp.prompt_tokens
        attrs["completion_tokens"] = resp.completion_tokens
        attrs["cost_usd"] = resp.cost_usd
        attrs["cache_hit"] = resp.cache_hit
        attrs["replayed"] = resp.replayed

        # Gated payload capture: keep prompts/completions out of spans by default;
        # when enabled, offload anything over 32 KB to MinIO and reference by URI.
        if _span_payloads_enabled():
            payload_id = new_id()
            await maybe_offload(attrs, "prompt", json.dumps(messages), blob_store, payload_id)
            await maybe_offload(attrs, "completion", resp.text, blob_store, payload_id)

    return resp
