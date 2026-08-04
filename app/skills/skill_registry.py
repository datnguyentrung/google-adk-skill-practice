from dataclasses import dataclass
from importlib import import_module
from typing import Any


@dataclass(frozen=True)
class SkillSpec:
    code: str
    skill_path: str
    tools_path: str


SKILL_REGISTRY: dict[str, SkillSpec] = {
    "calculate": SkillSpec(
        code="calculate",
        skill_path="app.skills.calculate:calculate_skill",
        tools_path="app.tools.calculate_tool:get_calculate_tools",
    ),
    "navigation": SkillSpec(
        code="navigation",
        skill_path="app.skills.navigation:navigation_skill",
        tools_path="app.tools.navigation_tools:get_navigation_tools",
    ),
    "cooking": SkillSpec(
        code="cooking",
        skill_path="app.skills.cooking:cooking_skill",
        tools_path="app.tools.cooking_tools:get_cooking_tools",
    ),
    # "hello-world": SkillSpec(
    #     code="hello-world",
    #     skill_path="app.skills.hello-world:hello_world_skill",
    #     tools_path="app.tools.hello_world_tool:get_hello_world_tools",
    # ),
}


def load_object(path: str) -> Any:
    module_path, object_name = path.split(":", maxsplit=1)
    module = import_module(module_path)
    return getattr(module, object_name)


def load_skill(code: str):
    spec = SKILL_REGISTRY[code]
    return load_object(spec.skill_path)


def load_tools(code: str) -> list:
    spec = SKILL_REGISTRY[code]
    tools_loader = load_object(spec.tools_path)
    return list(tools_loader())
