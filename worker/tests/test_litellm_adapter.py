"""LiteLLM adapter: cache miss/hit, --no-cache, defaults, and span attributes."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from helix.logging import SpanLogger
from helix.tools.litellm_adapter import LLMResponse, llm_call
from helix.tools.llm_cache import LLMCache

_MSGS = [{"role": "user", "content": "hi"}]


def _raw(text: str = "hello", prompt: int = 10, completion: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


class FakeLLM:
    """Stand-in for ``litellm.acompletion`` that records its calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return _raw()


def _fake_cost(_raw: Any) -> float:
    return 0.02


@pytest.fixture(autouse=True)
def _dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cache is on by default in dev; make that explicit and reproducible.
    monkeypatch.delenv("HELIX_ENV", raising=False)
    monkeypatch.delenv("HELIX_LLM_CACHE", raising=False)


def test_cache_key_is_stable_and_param_sensitive() -> None:
    cache = LLMCache()
    base = dict(model="m", messages=_MSGS, temperature=0.0, max_tokens=None, top_p=None, stop=None)
    assert cache.key(**base) == cache.key(**base)
    assert cache.key(**{**base, "temperature": 0.5}) != cache.key(**base)
    assert cache.key(**{**base, "stop": ["END"]}) != cache.key(**base)


async def test_miss_then_hit(tmp_path: Path) -> None:
    cache = LLMCache(tmp_path / "llm_cache")
    logger = SpanLogger(tmp_path / "spans.jsonl")
    fake = FakeLLM()

    first = await llm_call(
        _MSGS,
        model="claude-x",
        cache=cache,
        span_logger=logger,
        completion_fn=fake,
        cost_fn=_fake_cost,
    )
    assert isinstance(first, LLMResponse)
    assert (first.cache_hit, first.replayed) == (False, False)
    assert first.cost_usd == 0.02
    assert (first.prompt_tokens, first.completion_tokens) == (10, 5)
    assert len(fake.calls) == 1

    second = await llm_call(
        _MSGS,
        model="claude-x",
        cache=cache,
        span_logger=logger,
        completion_fn=fake,
        cost_fn=_fake_cost,
    )
    assert (second.cache_hit, second.replayed) == (True, True)
    assert second.cost_usd == 0.0  # zeroed so reruns don't over-count
    assert (second.prompt_tokens, second.completion_tokens) == (10, 5)  # replayed
    assert len(fake.calls) == 1  # LiteLLM not called again

    spans = [json.loads(line) for line in (tmp_path / "spans.jsonl").read_text().splitlines()]
    assert [s["kind"] for s in spans] == ["llm", "llm"]
    assert spans[0]["attributes"]["cache_hit"] is False
    assert spans[1]["attributes"]["cache_hit"] is True
    assert spans[1]["attributes"]["replayed"] is True
    assert spans[1]["attributes"]["cost_usd"] == 0.0
    assert spans[0]["attributes"]["model"] == "claude-x"


async def test_no_cache_bypasses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIX_LLM_CACHE", "0")
    cache = LLMCache(tmp_path / "llm_cache")
    logger = SpanLogger(tmp_path / "spans.jsonl")
    fake = FakeLLM()

    for _ in range(2):
        await llm_call(
            _MSGS,
            model="m",
            cache=cache,
            span_logger=logger,
            completion_fn=fake,
            cost_fn=_fake_cost,
        )
    assert len(fake.calls) == 2  # disabled cache → every call hits LiteLLM
    assert not (tmp_path / "llm_cache").exists()  # nothing written


async def test_temperature_defaults_to_zero(tmp_path: Path) -> None:
    fake = FakeLLM()
    await llm_call(
        _MSGS,
        model="m",
        cache=LLMCache(tmp_path / "c"),
        span_logger=SpanLogger(tmp_path / "s.jsonl"),
        completion_fn=fake,
        cost_fn=_fake_cost,
    )
    assert fake.calls[0]["temperature"] == 0.0


async def test_model_resolves_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIX_DEFAULT_MODEL", "env-model")
    fake = FakeLLM()
    resp = await llm_call(
        _MSGS,
        cache=LLMCache(tmp_path / "c"),
        span_logger=SpanLogger(tmp_path / "s.jsonl"),
        completion_fn=fake,
        cost_fn=_fake_cost,
    )
    assert resp.model == "env-model"
    assert fake.calls[0]["model"] == "env-model"


async def test_num_retries_forwarded_to_litellm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HELIX_LLM_NUM_RETRIES", raising=False)
    fake = FakeLLM()
    common = dict(
        cache=LLMCache(tmp_path / "c"),
        span_logger=SpanLogger(tmp_path / "s.jsonl"),
        completion_fn=fake,
        cost_fn=_fake_cost,
    )
    # Default retry budget is handed to LiteLLM so it owns transient-error backoff.
    await llm_call(_MSGS, model="m", **common)  # type: ignore[arg-type]
    assert fake.calls[0]["num_retries"] == 4

    # Env override is honored; explicit argument wins over the env.
    monkeypatch.setenv("HELIX_LLM_NUM_RETRIES", "7")
    await llm_call([{"role": "user", "content": "b"}], model="m", **common)  # type: ignore[arg-type]
    assert fake.calls[1]["num_retries"] == 7
    await llm_call([{"role": "user", "content": "c"}], model="m", num_retries=1, **common)  # type: ignore[arg-type]
    assert fake.calls[2]["num_retries"] == 1
