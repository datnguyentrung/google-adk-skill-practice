from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.runners import InMemoryRunner
from google.genai import types

logger = logging.getLogger(__name__)

DEFAULT_MODEL_RPM_BUDGET = max(
    0, int(os.getenv("INGESTION_MODEL_RPM_BUDGET", "10"))
)
DEFAULT_MODEL_RETRY_ATTEMPTS = max(
    1, int(os.getenv("INGESTION_MODEL_RETRY_ATTEMPTS", "4"))
)


class StageLocalModelCallExhausted(RuntimeError):
    """An ADK model call exhausted its transport retries."""

    stage_local_retries_exhausted = True

    def __init__(self, message: str, *, cause: Exception):
        super().__init__(message)
        self.__cause__ = cause
        self.status_code = getattr(cause, "status_code", None)


class ModelRequestPacer:
    """Thread-safe leaky-bucket pacer shared by model name."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def acquire(
        self,
        *,
        model: str,
        rpm_budget: int,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rpm_budget <= 0:
            return
        interval = 60.0 / rpm_budget
        with self._lock:
            now = self._clock()
            scheduled = max(now, self._next_allowed.get(model, now))
            self._next_allowed[model] = scheduled + interval
        delay = scheduled - now
        if delay > 0:
            logger.info(
                "[MODEL_RATE_PACE] model=%s rpm_budget=%s delay_seconds=%.2f",
                model,
                rpm_budget,
                delay,
            )
            sleep(delay)


_SHARED_PACER = ModelRequestPacer()


def _is_transport_exhaustion(exc: Exception) -> bool:
    message = str(exc).upper()
    status_code = getattr(exc, "status_code", None)
    if status_code in {408, 429, 500, 502, 503, 504}:
        return True
    return any(
        token in message
        for token in ("RESOURCE_EXHAUSTED", "TIMEOUT", "CONNECTION", "SERVERERROR")
    )


def _agent_name(operation: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", operation).strip("_")
    return f"ingestion_{safe or 'structured_call'}"


def _await_sync(coro):
    """Run an ADK coroutine from synchronous ingestion code, even inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def target() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - re-raised below
            error["value"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if error:
        raise error["value"]
    return result.get("value")


class AdkStructuredCallExecutor:
    """Run one structured LLM task through Google ADK."""

    def __init__(
        self,
        *,
        model: str,
        rpm_budget: int = DEFAULT_MODEL_RPM_BUDGET,
        retry_attempts: int = DEFAULT_MODEL_RETRY_ATTEMPTS,
        pacer: ModelRequestPacer = _SHARED_PACER,
        sleep: Callable[[float], None] = time.sleep,
        runner_factory: Callable[..., Any] = InMemoryRunner,
    ):
        self.model = model
        self.rpm_budget = rpm_budget
        self.retry_attempts = retry_attempts
        self.pacer = pacer
        self.sleep = sleep
        self.runner_factory = runner_factory

    def run(
        self,
        *,
        operation: str,
        instruction: str,
        output_schema: dict[str, Any] | type,
        message: str = "Produce the structured result now.",
    ) -> Any:
        self.pacer.acquire(
            model=self.model,
            rpm_budget=self.rpm_budget,
            sleep=self.sleep,
        )
        model = Gemini(
            model=self.model,
            retry_options=types.HttpRetryOptions(
                attempts=self.retry_attempts,
                initial_delay=2,
                max_delay=30,
                http_status_codes=[408, 429, 500, 502, 503, 504],
            ),
        )
        output_key = "structured_result"

        def apply_json_schema(callback_context, llm_request):
            del callback_context
            # ADK output_schema uses Google response_schema, which supports a
            # smaller schema dialect. Inject native response_json_schema so the
            # full dynamic JSON Schema (including numeric enums) is preserved.
            llm_request.config.response_mime_type = "application/json"
            llm_request.config.response_json_schema = output_schema
            llm_request.config.temperature = 0
            return None

        agent = Agent(
            name=_agent_name(operation),
            model=model,
            instruction=instruction,
            output_key=output_key,
            include_contents="none",
            before_model_callback=apply_json_schema,
            generate_content_config=types.GenerateContentConfig(temperature=0),
        )
        runner = self.runner_factory(
            agent=agent,
            app_name="ingestion_structured",
        )
        result = None
        final_text = ""
        session_id = f"{operation}-{uuid.uuid4().hex}"
        try:
            _await_sync(
                runner.session_service.create_session(
                    app_name="ingestion_structured",
                    user_id="ingestion",
                    session_id=session_id,
                )
            )
            events = runner.run(
                user_id="ingestion",
                session_id=session_id,
                new_message=types.Content(
                    role="user",
                    parts=[types.Part(text=message)],
                ),
            )
            for event in events:
                state_delta = getattr(
                    getattr(event, "actions", None),
                    "state_delta",
                    {},
                ) or {}
                if output_key in state_delta:
                    result = state_delta[output_key]
                if event.is_final_response() and event.content:
                    final_text = "".join(
                        part.text or ""
                        for part in event.content.parts or []
                        if not getattr(part, "thought", False)
                    )
        except Exception as exc:
            if _is_transport_exhaustion(exc):
                raise StageLocalModelCallExhausted(
                    f"ADK model retries exhausted for {operation}",
                    cause=exc,
                ) from exc
            raise
        finally:
            _await_sync(runner.close())
        if result is not None:
            return json.loads(result) if isinstance(result, str) else result
        if final_text.strip():
            return json.loads(final_text)
        raise RuntimeError(f"ADK agent returned no structured output for {operation}")


__all__ = [
    "AdkStructuredCallExecutor",
    "DEFAULT_MODEL_RPM_BUDGET",
    "ModelRequestPacer",
    "StageLocalModelCallExhausted",
]
