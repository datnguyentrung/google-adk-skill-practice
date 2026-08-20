from pathlib import Path

from google.adk.agents import Agent
from google.adk.tools.skill_toolset import SkillToolset

from app.skills.root_prompt_renderer import render_root_agent_prompt
from app.skills.skill_loader import discover_skills

BASE_DIR = Path(__file__).resolve().parent
SKILLS_DIR = BASE_DIR / "skills"

ROOT_AGENT_PROMPT_PATH = BASE_DIR / "prompts" / "root_agent_prompt.md"


def create_root_agent() -> Agent:
    loaded_skills = discover_skills(SKILLS_DIR)

    if not loaded_skills:
        raise RuntimeError(f"No valid SKILL.md files were found under {SKILLS_DIR}.")

    skills = [loaded.skill for loaded in loaded_skills]

    additional_tools = [tool for loaded in loaded_skills for tool in loaded.tools]

    root_instruction = render_root_agent_prompt(
        prompt_path=ROOT_AGENT_PROMPT_PATH,
        loaded_skills=loaded_skills,
    )

    skill_toolset = SkillToolset(
        skills=skills,
        additional_tools=additional_tools,
    )

    return Agent(
        name="root_agent",
        model="gemini-3.1-flash-lite",
        description=(
            "A root agent that dynamically routes requests to available skills."
        ),
        instruction=root_instruction,
        tools=[skill_toolset],
    )


root_agent = create_root_agent()
root_skill_toolset = root_agent.tools[0]
