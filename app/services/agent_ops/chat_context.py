

import inspect
from functools import wraps
from typing import Any, Callable

from opentelemetry import trace


def apply_agentops_chat_context(extra: dict[str, Any] | None = None) -> None:
    """Attach common ADK/AgentOps attributes to the current span when present."""
    span = trace.get_current_span()
    if span is None or not span.is_recording():
        return

    for key, value in (extra or {}).items():
        if value is not None:
            span.set_attribute(key, value)


def with_agentops_span_context(
    *,
    span_name: str,
    tool_name: str | None = None,
    operation_name: str | None = None,
    extra: dict[str, Any] | None = None,
):
    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        attrs = {
            "agentops.span.name": span_name,
            "agentops.tool.name": tool_name,
            "agentops.operation.name": operation_name,
            **(extra or {}),
        }

        if inspect.isasyncgenfunction(func):

            @wraps(func)
            async def asyncgen_wrapper(*args, **kwargs):
                apply_agentops_chat_context(attrs)
                async for item in func(*args, **kwargs):
                    yield item

            return asyncgen_wrapper

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                apply_agentops_chat_context(attrs)
                return await func(*args, **kwargs)

            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            apply_agentops_chat_context(attrs)
            return func(*args, **kwargs)

        return sync_wrapper

    return decorate
