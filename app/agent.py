from pathlib import Path

from google.adk.agents import Agent
from google.adk.tools.skill_toolset import SkillToolset
from typing_extensions import Iterable

from app.services.agent_ops.adk_instrumentation import build_agent
from app.skills.skill_registry import load_skill, load_tools

BASE_DIR = Path(__file__).resolve().parent
SKILLS_DIR = BASE_DIR / "skills"

ROOT_AGENT_PROMPT_PATH = BASE_DIR / "prompts" / "root_agent_prompt.md"


def create_root_agent(
    selected_skill_codes: Iterable[str],
) -> Agent:
    codes = list(dict.fromkeys(selected_skill_codes))

    if not codes:
        raise ValueError("At least one skill must be selected.")

    selected_skills = [load_skill(code) for code in codes]

    selected_tools = [tool for code in codes for tool in load_tools(code)]

    selected_skill_toolset = SkillToolset(
        skills=selected_skills,
        additional_tools=selected_tools,
    )

    return build_agent(
        Agent,
        name="hello_world_agent",
        model="gemini-3.1-flash-lite",
        description=(
            "A friendly agent that routes greetings, calculations, and urban navigation."
        ),
        instruction=ROOT_AGENT_PROMPT_PATH.read_text(encoding="utf-8"),
        tools=[selected_skill_toolset],
    )


#
root_agent = create_root_agent(
    selected_skill_codes=["calculate", "navigation", "hello-world"]
)
