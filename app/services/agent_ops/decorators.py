

import inspect
from functools import wraps
from typing import Any, Callable

from agentops.sdk.decorators import agent as agentops_agent
from agentops.sdk.decorators import operation as agentops_operation
from agentops.sdk.decorators import tool as agentops_tool
from agentops.sdk.decorators import trace as agentops_trace

from app.services.agent_ops.chat_context import with_agentops_span_context


def agent(*args, **kwargs):
    return agentops_agent(*args, **kwargs)


def _resolve_span_name(
    decorator_kwargs: dict[str, Any],
    func: Callable[..., Any],
) -> str:
    configured_name = decorator_kwargs.get("name")
    return str(configured_name or getattr(func, "__name__", "unknown"))


def _wrap_span_entry(
    func: Callable[..., Any],
    *,
    span_kind: str,
    span_name: str,
) -> Callable[..., Any]:
    context_decorator = with_agentops_span_context(
        span_name=span_name,
        tool_name=span_name if span_kind == "tool" else None,
        operation_name=span_name if span_kind in {"operation", "trace"} else None,
        extra={
            "agentops.span.kind": span_kind,
            "shopping.span.kind": span_kind,
        },
    )

    wrapped = context_decorator(func)
    if inspect.isasyncgenfunction(wrapped):
        @wraps(wrapped)
        async def asyncgen_entry(*args, **kwargs):
            async for item in wrapped(*args, **kwargs):
                yield item

        return asyncgen_entry

    if inspect.iscoroutinefunction(wrapped):
        @wraps(wrapped)
        async def async_entry(*args, **kwargs):
            return await wrapped(*args, **kwargs)

        return async_entry

    @wraps(wrapped)
    def sync_entry(*args, **kwargs):
        return wrapped(*args, **kwargs)

    return sync_entry


def _build_wrapped_decorator(
    base_decorator: Callable[..., Any],
    span_kind: str,
):
    def factory(*decorator_args, **decorator_kwargs):
        if decorator_args and callable(decorator_args[0]) and len(decorator_args) == 1 and not decorator_kwargs:
            func = decorator_args[0]
            span_name = _resolve_span_name({}, func)
            return base_decorator()(_wrap_span_entry(func, span_kind=span_kind, span_name=span_name))

        def apply(func: Callable[..., Any]):
            span_name = _resolve_span_name(decorator_kwargs, func)
            wrapped = _wrap_span_entry(func, span_kind=span_kind, span_name=span_name)
            return base_decorator(*decorator_args, **decorator_kwargs)(wrapped)

        return apply

    return factory


trace = _build_wrapped_decorator(agentops_trace, "trace")
operation = _build_wrapped_decorator(agentops_operation, "operation")
tool = _build_wrapped_decorator(agentops_tool, "tool")
