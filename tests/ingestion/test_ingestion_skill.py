import asyncio

from google.adk.agents.invocation_context import (
    InvocationContext,
    new_invocation_context_id,
)
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.agents.run_config import RunConfig
from google.adk.sessions import InMemorySessionService

from app.agent import root_agent, root_skill_toolset
from app.tools.ingestion_tools import INGESTION_TOOLS

INGESTION_TOOL_NAMES = {
    tool.__name__ for tool in INGESTION_TOOLS.values()
}


def test_ingestion_skill_loads_with_exact_dynamic_tools():
    async def run():
        registry = root_skill_toolset._registry
        assert registry is not None

        skill = await registry.get_skill(name="ingestion")
        declared = set(
            skill.frontmatter.metadata["adk_additional_tools"]
        )

        assert declared == INGESTION_TOOL_NAMES
        assert "$" not in skill.instructions
        assert "prepare_extraction_context" in skill.instructions
        assert "validate_graph_patch" in skill.instructions
        assert "fill_graph_patch" in skill.instructions

    asyncio.run(run())


def test_root_agent_exposes_ingestion_tools_only_after_activation():
    async def run():
        inactive_names = {
            tool.name for tool in await root_skill_toolset.get_tools(None)
        }
        assert inactive_names.isdisjoint(INGESTION_TOOL_NAMES)

        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name="test",
            user_id="user",
            session_id="ingestion-session",
            state={
                f"_adk_activated_skill_{root_agent.name}": ["ingestion"]
            },
        )
        invocation_context = InvocationContext(
            session_service=session_service,
            session=session,
            invocation_id=new_invocation_context_id(),
            agent=root_agent,
            run_config=RunConfig(),
        )
        active_names = {
            tool.name
            for tool in await root_skill_toolset.get_tools(
                ReadonlyContext(invocation_context)
            )
        }
        assert INGESTION_TOOL_NAMES <= active_names

    asyncio.run(run())
    assert root_skill_toolset.skills == []
