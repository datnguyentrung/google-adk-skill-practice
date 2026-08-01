from __future__ import annotations

from typing import Any

from google.adk.tools.skill_toolset import SkillToolset

from app.services.agent_ops.agent_ops import init_agentops
from app.services.agent_ops.decorators import agent, operation, tool, trace


def _context_value(context: Any, name: str, default: str = "unknown") -> str:
    value = getattr(context, name, None)
    return str(value if value is not None else default)


def _decorate_adk_tool(adk_tool: Any) -> Any:
    if getattr(adk_tool, "_agentops_decorated", False):
        return adk_tool

    original_run_async = adk_tool.run_async
    tool_name = getattr(adk_tool, "name", type(adk_tool).__name__)

    @tool(name=f"adk_tool.{tool_name}")
    async def run_async(*, args: dict[str, Any], tool_context: Any) -> Any:
        return await original_run_async(args=args, tool_context=tool_context)

    adk_tool.run_async = run_async
    adk_tool._agentops_decorated = True
    return adk_tool


class AgentOpsSkillToolset(SkillToolset):
    """SkillToolset that decorates generated ADK skill tools with AgentOps."""

    @operation(name="adk_skill_toolset.get_tools")
    async def get_tools(self, readonly_context: Any | None = None):
        tools = await super().get_tools(readonly_context)
        return [_decorate_adk_tool(adk_tool) for adk_tool in tools]


@operation(name="adk_model.before")
def before_model_callback(callback_context: Any, llm_request: Any) -> None:
    return None


@operation(name="adk_model.after")
def after_model_callback(callback_context: Any, llm_response: Any) -> None:
    return None


@operation(name="adk_tool.before")
def before_tool_callback(tool: Any, args: dict[str, Any], tool_context: Any) -> None:
    return None


@operation(name="adk_tool.after")
def after_tool_callback(
    tool: Any,
    args: dict[str, Any],
    tool_context: Any,
    tool_response: dict[str, Any],
) -> None:
    return None


@trace(name="create_hello_world_agent")
@agent(name="Hello World ADK Agent")
def build_agent(agent_cls: type, **kwargs: Any):
    init_agentops()
    return agent_cls(
        before_model_callback=before_model_callback,
        after_model_callback=after_model_callback,
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
        **kwargs,
    )
