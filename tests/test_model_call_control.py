from types import SimpleNamespace

import pytest
from google.genai import types

from app.services.ingestion.model_call_control import (
    AdkStructuredCallExecutor,
    ModelRequestPacer,
    StageLocalModelCallExhausted,
    StructuredModelOutputError,
)


class _RateLimitError(Exception):
    status_code = 429


class _Event:
    def __init__(self, result=None):
        self.actions = SimpleNamespace(
            state_delta={"structured_result": result} if result is not None else {}
        )
        self.content = None

    def is_final_response(self):
        return True


class _FakeSessionService:
    def __init__(self):
        self.created = []

    async def create_session(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(**kwargs)


class _FakeRunner:
    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error
        self.closed = False
        self.session_service = _FakeSessionService()

    def run(self, **_kwargs):
        if self.error is not None:
            raise self.error
        return [_Event(self.result)]

    async def close(self):
        self.closed = True


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


def test_adk_executor_returns_structured_state_output():
    runner = _FakeRunner(result={"ok": True})
    seen = {}

    def runner_factory(**kwargs):
        seen.update(kwargs)
        return runner

    executor = AdkStructuredCallExecutor(
        model="gemini-3.1-flash-lite",
        rpm_budget=0,
        runner_factory=runner_factory,
    )
    result = executor.run(
        operation="mapping",
        instruction="Return a structured result.",
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
    )

    assert result == {"ok": True}
    assert seen["agent"].output_key == "structured_result"
    request = SimpleNamespace(config=types.GenerateContentConfig())
    seen["agent"].before_model_callback(callback_context=None, llm_request=request)
    assert request.config.response_json_schema["properties"]["ok"]["type"] == "boolean"
    assert request.config.thinking_config.thinking_level == "MEDIUM"
    assert (
        seen["agent"].generate_content_config.thinking_config.thinking_level
        == "MEDIUM"
    )
    assert runner.closed is True


def test_adk_transport_exhaustion_is_marked_stage_local():
    error = _RateLimitError("quota exhausted")
    runner = _FakeRunner(error=error)
    executor = AdkStructuredCallExecutor(
        model="gemini-3.1-flash-lite",
        rpm_budget=0,
        runner_factory=lambda **_kwargs: runner,
    )

    with pytest.raises(StageLocalModelCallExhausted) as caught:
        executor.run(
            operation="mapping",
            instruction="Return structured output.",
            output_schema={"type": "object"},
        )

    assert caught.value.stage_local_retries_exhausted is True
    assert caught.value.status_code == 429
    assert runner.closed is True


def test_missing_structured_output_is_retryable():
    runner = _FakeRunner(result=None)
    executor = AdkStructuredCallExecutor(
        model="gemini-test", rpm_budget=0, runner_factory=lambda **_kwargs: runner
    )
    with pytest.raises(StructuredModelOutputError) as caught:
        executor.run(operation="mapping", instruction="Return JSON.", output_schema={"type": "object"})
    assert caught.value.retryable is True
    assert caught.value.error_kind == "llm_output"
    assert runner.closed is True
