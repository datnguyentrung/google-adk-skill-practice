from types import SimpleNamespace

import pytest

from app.services.ingestion.model_call_control import (
    GeminiCallExecutor,
    ModelRequestPacer,
    StageLocalModelCallExhausted,
)


class _RateLimitError(Exception):
    status_code = 429


class _FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(parsed=outcome)


class _FakeClient:
    def __init__(self, outcomes):
        self.models = _FakeModels(outcomes)


def test_model_pacer_spaces_requests_by_rpm_budget():
    state = {"now": 0.0}
    sleeps = []

    def clock():
        return state["now"]

    def sleep(delay):
        sleeps.append(delay)
        state["now"] += delay

    pacer = ModelRequestPacer(clock=clock)
    pacer.acquire(model="gemini-test", rpm_budget=10, sleep=sleep)
    pacer.acquire(model="gemini-test", rpm_budget=10, sleep=sleep)
    pacer.acquire(model="gemini-test", rpm_budget=10, sleep=sleep)

    assert sleeps == [6.0, 6.0]


def test_rate_limit_retries_the_same_model_call_locally():
    client = _FakeClient(
        [_RateLimitError("Please retry in 0s"), {"ok": True}]
    )
    sleeps = []
    executor = GeminiCallExecutor(
        client=client,
        model="gemini-test",
        rpm_budget=0,
        max_rate_limit_retries=1,
        sleep=sleeps.append,
    )
    result = executor.generate_content(
        operation="coverage",
        contents="same prompt",
        config={"temperature": 0},
    )

    assert result.parsed == {"ok": True}
    assert len(client.models.calls) == 2
    assert client.models.calls[0] == client.models.calls[1]
    assert sleeps == [4.0]


def test_exhausted_rate_limit_is_marked_stage_local():
    error = _RateLimitError("quota exhausted")
    executor = GeminiCallExecutor(
        client=_FakeClient([error]),
        model="gemini-test",
        rpm_budget=0,
        max_rate_limit_retries=0,
        sleep=lambda _delay: None,
    )

    with pytest.raises(StageLocalModelCallExhausted) as caught:
        executor.generate_content(
            operation="selector",
            contents="prompt",
            config={},
        )

    assert caught.value.stage_local_retries_exhausted is True
    assert caught.value.status_code == 429
