

from typing import Any, Optional

from opentelemetry import trace

from app.services.agent_ops.chat_context import apply_agentops_chat_context


def _to_int(value: object, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def set_agentops_llm_usage(
    *,
    input_tokens: Optional[int | str] = None,
    output_tokens: Optional[int | str] = None,
    prompt_tokens: Optional[int | str] = None,
    completion_tokens: Optional[int | str] = None,
    total_tokens: Optional[int | str] = None,
    model: Optional[str] = None,
    provider: str = "gemini",
) -> None:
    """
    Normalize token usage cho AgentOps/OpenTelemetry span hiện tại.

    Dùng khi provider/ADK trả token dạng:
        input_tokens / output_tokens

    nhưng AgentOps UI cần:
        prompt_tokens / completion_tokens / total_tokens
    """

    prompt = _to_int(prompt_tokens if prompt_tokens is not None else input_tokens)
    completion = _to_int(
        completion_tokens if completion_tokens is not None else output_tokens
    )
    total = _to_int(total_tokens, prompt + completion)

    span = trace.get_current_span()

    if span is None or not span.is_recording():
        return

    apply_agentops_chat_context()

    # Format AgentOps/Gemini instrumentation đang hiển thị được trên UI
    span.set_attribute("gen_ai.usage.prompt_tokens", str(prompt))
    span.set_attribute("gen_ai.usage.completion_tokens", str(completion))
    span.set_attribute("gen_ai.usage.total_tokens", str(total))

    # Giữ lại format cũ để không mất dữ liệu ADK/Vertex
    # Một số backend observability khác đọc semantic convention kiểu này
    span.set_attribute("llm.token_count.prompt", prompt)
    span.set_attribute("llm.token_count.completion", completion)
    span.set_attribute("llm.token_count.total", total)

    if model:
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("llm.model_name", model)

    span.set_attribute("business.cost.countable", True)
    span.set_attribute("business.cost.source", "provider_llm_span")
    span.set_attribute("gen_ai.system", provider)
    span.set_attribute("llm.provider", provider)
    if model:
        span.set_attribute("llm.model", model)


def _get_usage_value(usage: Any, *keys: str) -> object:
    for key in keys:
        if isinstance(usage, dict) and key in usage:
            return usage.get(key)
        if hasattr(usage, key):
            return getattr(usage, key)
    return None


def set_agentops_llm_usage_from_response(
    response: Any,
    *,
    model: Optional[str] = None,
    provider: str = "gemini",
) -> None:
    usage = (
        getattr(response, "usage_metadata", None)
        or getattr(response, "usage", None)
        or getattr(response, "response_metadata", None)
    )

    if isinstance(usage, dict) and "token_usage" in usage:
        usage = usage["token_usage"]

    if not usage:
        return

    set_agentops_llm_usage(
        input_tokens=_get_usage_value(
            usage,
            "input_tokens",
            "prompt_tokens",
            "prompt_token_count",
        ),
        output_tokens=_get_usage_value(
            usage,
            "output_tokens",
            "completion_tokens",
            "candidates_token_count",
        ),
        total_tokens=_get_usage_value(
            usage,
            "total_tokens",
            "total_token_count",
        ),
        model=model,
        provider=provider,
    )
