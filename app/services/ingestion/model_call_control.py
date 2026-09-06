from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MODEL_RPM_BUDGET = max(
    0, int(os.getenv("INGESTION_MODEL_RPM_BUDGET", "10"))
)
DEFAULT_MODEL_RATE_LIMIT_RETRIES = max(
    0, int(os.getenv("INGESTION_MODEL_RATE_LIMIT_RETRIES", "3"))
)
DEFAULT_MODEL_TRANSPORT_RETRIES = max(
    0, int(os.getenv("INGESTION_MODEL_TRANSPORT_RETRIES", "2"))
)


class StageLocalModelCallExhausted(RuntimeError):
    """A model call exhausted local retries and must not restart the pipeline."""

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


def _is_rate_limit_error(exc: Exception) -> bool:
    message = str(exc).upper()
    status_code = getattr(exc, "status_code", None)
    return status_code == 429 or (
        "429" in message and ("RESOURCE_EXHAUSTED" in message or "QUOTA" in message)
    )


def _is_transient_transport_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            httpx.RemoteProtocolError,
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.ReadError,
            httpx.WriteError,
        ),
    )


def _retry_delay_seconds(exc: Exception, attempt: int) -> float:
    match = re.search(
        r"(?:Please retry in|retryDelay['\": ]+)[^0-9]*([0-9.]+)s",
        str(exc),
        flags=re.IGNORECASE,
    )
    server_delay = float(match.group(1)) if match else 0.0
    backoff = min(60.0, 4.0 * (2 ** max(0, attempt - 1)))
    return max(server_delay, backoff)


def _transport_delay_seconds(attempt: int) -> float:
    return min(30.0, 2.0 * (2 ** max(0, attempt - 1)))


class GeminiCallExecutor:
    """Rate-aware, stage-local retry wrapper for synchronous Gemini calls."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        rpm_budget: int = DEFAULT_MODEL_RPM_BUDGET,
        max_rate_limit_retries: int = DEFAULT_MODEL_RATE_LIMIT_RETRIES,
        max_transport_retries: int = DEFAULT_MODEL_TRANSPORT_RETRIES,
        pacer: ModelRequestPacer = _SHARED_PACER,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.model = model
        self.rpm_budget = rpm_budget
        self.max_rate_limit_retries = max_rate_limit_retries
        self.max_transport_retries = max_transport_retries
        self.pacer = pacer
        self.sleep = sleep

    def generate_content(self, *, operation: str, **kwargs: Any) -> Any:
        rate_retries = 0
        transport_retries = 0
        while True:
            self.pacer.acquire(
                model=self.model,
                rpm_budget=self.rpm_budget,
                sleep=self.sleep,
            )
            try:
                return self.client.models.generate_content(
                    model=self.model,
                    **kwargs,
                )
            except Exception as exc:
                if _is_rate_limit_error(exc):
                    if rate_retries >= self.max_rate_limit_retries:
                        raise StageLocalModelCallExhausted(
                            f"Rate-limit retries exhausted for {operation}",
                            cause=exc,
                        ) from exc
                    rate_retries += 1
                    delay = _retry_delay_seconds(exc, rate_retries)
                    logger.warning(
                        "[MODEL_CALL_RATE_LIMIT_RETRY] operation=%s model=%s "
                        "retry=%s delay_seconds=%.2f",
                        operation,
                        self.model,
                        rate_retries,
                        delay,
                    )
                    self.sleep(delay)
                    continue
                if _is_transient_transport_error(exc):
                    if transport_retries >= self.max_transport_retries:
                        raise StageLocalModelCallExhausted(
                            f"Transport retries exhausted for {operation}",
                            cause=exc,
                        ) from exc
                    transport_retries += 1
                    delay = _transport_delay_seconds(transport_retries)
                    logger.warning(
                        "[MODEL_CALL_TRANSPORT_RETRY] operation=%s model=%s "
                        "retry=%s delay_seconds=%.2f error=%s",
                        operation,
                        self.model,
                        transport_retries,
                        delay,
                        type(exc).__name__,
                    )
                    self.sleep(delay)
                    continue
                raise


__all__ = [
    "DEFAULT_MODEL_RPM_BUDGET",
    "GeminiCallExecutor",
    "ModelRequestPacer",
    "StageLocalModelCallExhausted",
]
