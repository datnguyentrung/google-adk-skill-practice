"""Helpers for loading ADK SKILL.md files as rendered templates."""

from pathlib import Path
from string import Template
from typing import Any, Mapping

from google.adk import skills
from google.adk.skills import models


def load_rendered_skill_from_dir(
    skill_dir: Path,
    substitutions: Mapping[str, Any],
) -> models.Skill:
    """Load a SKILL.md directory and render its instruction body."""

    skill = skills.load_skill_from_dir(skill_dir)
    instructions = Template(skill.instructions).substitute(
        {key: str(value) for key, value in substitutions.items()}
    )
    return models.Skill(
        frontmatter=skill.frontmatter,
        instructions=instructions,
        resources=skill.resources,
    )
