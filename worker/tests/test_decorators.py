"""Local-execution behavior for @task, @workflow, and gather."""

import helix


@helix.task(retries=2, timeout="30s")
async def double(x: int) -> int:
    return x * 2


@helix.task()
async def increment(x: int) -> int:
    return x + 1


@helix.workflow(name="toy", version="1.0.0")
async def toy(start: int) -> int:
    a, b = await helix.gather(double(start), increment(start))
    return a + b


async def test_workflow_runs_locally() -> None:
    # double(3)=6, increment(3)=4 -> 10
    assert await toy.local(start=3) == 10


async def test_gather_preserves_order() -> None:
    results = await helix.gather(increment(1), double(1), increment(10))
    assert results == [2, 2, 11]


def test_task_records_metadata() -> None:
    assert double.retries == 2
    assert double.timeout_s == 30.0
    assert increment.retries == 0
    assert increment.timeout_s is None


def test_workflow_records_metadata() -> None:
    assert toy.name == "toy"
    assert toy.version == "1.0.0"


def test_timeout_units() -> None:
    from helix.decorators import _parse_timeout

    assert _parse_timeout("45s") == 45.0
    assert _parse_timeout("2m") == 120.0
    assert _parse_timeout("1h") == 3600.0
    assert _parse_timeout(None) is None


def test_types_have_defaults() -> None:
    doc = helix.Doc(id="d1", text="hello", source="corpus")
    assert doc.score == 0.0
    assert doc.metadata == {}

    answer = helix.Answer(text="ok", citations=[helix.Citation(doc_id="d1", passage="hello")])
    assert answer.metadata == {}
    assert answer.citations[0].relevance == 0.0
