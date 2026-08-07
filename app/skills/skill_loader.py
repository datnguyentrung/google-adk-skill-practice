from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from google.adk import skills
from google.adk.skills import models


@dataclass(frozen=True)
class LoadedSkill:
    """A discovered ADK skill and its Python tools."""

    code: str
    directory: Path
    skill: models.Skill
    tools: tuple[Any, ...]


def discover_skills(skills_dir: Path) -> list[LoadedSkill]:
    """
    Discover every skill directory containing a SKILL.md file.

    A skill may optionally expose Python tools through:
        app.skills.<skill_code>.tools:get_tools
    """

    discovered: list[LoadedSkill] = []

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue

        if skill_dir.name.startswith("_"):
            continue

        skill_file = skill_dir / "SKILL.md"

        if not skill_file.is_file():
            continue

        skill_code = skill_dir.name
        loaded_skill = _load_skill(skill_dir)
        loaded_tools = _load_skill_tools(skill_code)

        discovered.append(
            LoadedSkill(
                code=skill_code,
                directory=skill_dir,
                skill=loaded_skill,
                tools=tuple(loaded_tools),
            )
        )
    return discovered


def _load_skill(skill_dir: Path) -> models.Skill:
    """Load a rendered Python skill when available, otherwise load SKILL.md."""

    skill_code = skill_dir.name
    module_name = f"app.skills.{skill_code}.{skill_code}"
    object_name = f"{skill_code.replace('-', '_')}_skill"

    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return skills.load_skill_from_dir(skill_dir)
        raise

    loaded_skill = getattr(module, object_name, None)

    if loaded_skill is None:
        return skills.load_skill_from_dir(skill_dir)

    if not isinstance(loaded_skill, models.Skill):
        raise RuntimeError(f"{module_name}:{object_name} must be an ADK Skill.")

    return loaded_skill


def _load_skill_tools(skill_code: str) -> list[Any]:
    """
    Load tools using the convention:

        app.skills.<skill_code>.tools:get_tools
    """

    module_names = [
        f"app.skills.{skill_code}.{skill_code}",
        f"app.skills.{skill_code}.tools",
    ]

    for module_name in module_names:
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as exc:
            # Skill có thể chỉ chứa prompt và không có Python tool.
            if exc.name == module_name:
                continue

            # Nếu lỗi xảy ra bên trong module thì phải báo lỗi thật.
            raise

        get_tools = getattr(module, "get_tools", None)

        if get_tools is None:
            continue

        return list(get_tools())

    return []
