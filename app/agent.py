import logging
import os
from pathlib import Path

from google.adk.agents import Agent
from google.adk.apps.app import App
from google.adk.plugins.save_files_as_artifacts_plugin import (
    SaveFilesAsArtifactsPlugin,
)
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types

from app.skills.local_skill_registry import LocalSkillRegistry
from app.skills.root_prompt_renderer import render_root_agent_prompt
from app.skills.skill_loader import (
    discover_skill_descriptors,
    discover_skill_tools,
)

from app.core.logging_config import configure_logging

BASE_DIR = Path(__file__).resolve().parent
SKILLS_DIR = BASE_DIR / "skills"
ROOT_AGENT_PROMPT_PATH = BASE_DIR / "prompts" / "root_agent_prompt.md"


def _configure_console_logging() -> None:
    configure_logging(logging.INFO)



def create_root_agent() -> Agent:
    descriptors = discover_skill_descriptors(SKILLS_DIR)

    if not descriptors:
        raise RuntimeError(f"No valid SKILL.md files were found under {SKILLS_DIR}.")

    additional_tools = discover_skill_tools(descriptors)
    registry = LocalSkillRegistry(descriptors)

    root_instruction = render_root_agent_prompt(
        prompt_path=ROOT_AGENT_PROMPT_PATH,
        skill_descriptors=descriptors,
    )

    skill_toolset = SkillToolset(
        skills=[],
        registry=registry,
        additional_tools=additional_tools,
    )

    return Agent(
        name="root_agent",
        model=os.getenv("GOOGLE_ADK_MODEL", "gemini-3.5-flash-lite"),
        description=(
            "A root agent that dynamically routes requests to available skills."
        ),
        instruction=root_instruction,
        tools=[skill_toolset],
        generate_content_config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level="MEDIUM"),
        ),
    )


_configure_console_logging()

root_agent = create_root_agent()
root_skill_toolset = root_agent.tools[0]

app = App(
    name="google_adk_skill_practice",
    root_agent=root_agent,
    plugins=[SaveFilesAsArtifactsPlugin()],
)
