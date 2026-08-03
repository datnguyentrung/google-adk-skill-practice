from pathlib import Path

from app.skills.calculate import calculate_skill
from app.skills.navigation import navigation_skill

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir

from app.services.agent_ops.adk_instrumentation import AgentOpsSkillToolset
from app.services.agent_ops.adk_instrumentation import build_agent

from app.tools.calculate_tool import calculate, prepare_calculation
from app.tools.navigation_tools import NAVIGATION_TOOLS

BASE_DIR = Path(__file__).resolve().parent
SKILLS_DIR = BASE_DIR / "skills"

hello_world_skill = load_skill_from_dir(SKILLS_DIR / "hello-world")


root_skill_toolset = AgentOpsSkillToolset(
    skills=[hello_world_skill, calculate_skill, navigation_skill],
    additional_tools=[
        prepare_calculation,
        calculate,
        *NAVIGATION_TOOLS.values(),
    ],
)

root_agent = build_agent(
    Agent,
    name="hello_world_agent",
    model="gemini-3.1-flash-lite",
    description=(
        "A friendly agent that routes greetings, calculations, and urban navigation."
    ),
    instruction=(
        """
        You are the root agent responsible for routing user requests to available skills.

        For simulated urban navigation requests, load the urban-navigation
        skill and follow its workflow in this agent. Extract fields from the
        current message, combine them with navigation session state, and call
        only the tool required for the next workflow step.

        For every request:

        1. Analyze the user's intent.
        2. Review the names and descriptions of the available skills.
        3. Select the single most appropriate skill for the request.
        4. Load that skill before performing the task.
        5. Follow the loaded skill's instructions exactly.
        6. Use the tools and resources required by the selected skill.
        7. Return only the useful final answer to the user.

        Rules:

        - Do not select a skill based only on a keyword. Consider the full intent.
        - Do not use an unrelated skill.
        - Do not invent capabilities that are not provided by the available skills.
        - Do not expose internal routing, skill loading, tool calls, or implementation details.
        - If no available skill can handle the request, state that the request is
        currently unsupported.
        - If multiple skills appear equally suitable and the user's intent is unclear,
        ask one concise clarification question.
    """
    ),
    tools=[root_skill_toolset],
)
