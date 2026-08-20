from pathlib import Path
from string import Template
from typing import Iterable

from app.skills.skill_loader import SkillDescriptor


def render_root_agent_prompt(
    prompt_path: Path,
    skill_descriptors: Iterable[SkillDescriptor],
) -> str:
    """Render the root prompt from lightweight skill metadata only."""

    catalog = build_skill_catalog(skill_descriptors)
    template = Template(prompt_path.read_text(encoding="utf-8"))

    return template.safe_substitute(
        skills_catalog=catalog,
    )


def build_skill_catalog(
    skill_descriptors: Iterable[SkillDescriptor],
) -> str:
    """Build the skill list shown to the root agent."""

    entries: list[str] = []

    for descriptor in skill_descriptors:
        entries.append(
            "\n".join(
                [
                    f"- Skill name: `{descriptor.name}`",
                    f"  Description: {descriptor.description}",
                ]
            )
        )

    return "\n\n".join(entries)
