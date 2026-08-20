from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from google.adk import skills
from google.adk.skills import models


@dataclass(frozen=True)
class SkillDescriptor:
    """Lightweight local skill metadata discovered from SKILL.md frontmatter."""

    code: str
    directory: Path
    frontmatter: models.Frontmatter

    @property
    def name(self) -> str:
        return self.frontmatter.name

    @property
    def description(self) -> str:
        return self.frontmatter.description


@dataclass(frozen=True)
class LoadedSkill:
    """A fully loaded ADK skill and its Python tools."""
    code: str
    directory: Path
    skill: models.Skill
    tools: tuple[Any, ...]


def discover_skill_descriptors(
    skills_dir: Path,
) -> list[SkillDescriptor]:
    """Discover only frontmatter metadata; do not load full skill bodies."""

    listed = skills.list_skills_in_dir(skills_dir)

    return [
        SkillDescriptor(
            code=code,
            directory=skills_dir / code,
            frontmatter=frontmatter,
        )
        for code, frontmatter in listed.items()
    ]


def discover_skill_tools(
    descriptors: list[SkillDescriptor],
) -> list[Any]:
    """Load only Python tool callables needed by SkillToolset."""

    return [
        tool
        for descriptor in descriptors
        for tool in _load_skill_tools(descriptor.code)
    ]


def load_skill_descriptor(
    descriptor: SkillDescriptor,
) -> models.Skill:
    """Load one complete skill on demand."""

    return _load_skill(descriptor.directory)


def discover_skills(skills_dir: Path) -> list[LoadedSkill]:
    """Backward-compatible eager loader for tests or explicit callers."""

    discovered: list[LoadedSkill] = []

    for descriptor in discover_skill_descriptors(skills_dir):
        discovered.append(
            LoadedSkill(
                code=descriptor.code,
                directory=descriptor.directory,
                skill=load_skill_descriptor(descriptor),
                tools=tuple(_load_skill_tools(descriptor.code)),
            )
        )

    return discovered


def _load_skill(skill_dir: Path) -> models.Skill:
    """Load a rendered Python skill when available, otherwise raw SKILL.md."""

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
        raise RuntimeError(
            f"{module_name}:{object_name} must be an ADK Skill."
        )

    return loaded_skill


def _load_skill_tools(skill_code: str) -> list[Any]:
    """Load tool callables without importing the full skill module."""

    module_name = f"app.skills.{skill_code}.tools"

    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return []
        raise

    get_tools = getattr(module, "get_tools", None)
    if get_tools is None:
        return []

    return list(get_tools())
