from pathlib import Path
from string import Template
from typing import Any, Iterable

from app.skills.skill_loader import LoadedSkill


def render_root_agent_prompt(
    prompt_path: Path,
    loaded_skills: Iterable[LoadedSkill],
) -> str:
    """Render the root-agent prompt with the discovered skill catalog."""

    catalog = build_skill_catalog(loaded_skills)

    template = Template(
        prompt_path.read_text(encoding="utf-8")
    )

    return template.safe_substitute(
        skills_catalog=catalog,
    )


def build_skill_catalog(
    loaded_skills: Iterable[LoadedSkill],
) -> str:
    """Build the skill list shown to the root agent."""

    entries: list[str] = []

    for loaded in loaded_skills:
        name = _read_frontmatter_value(
            loaded.skill.frontmatter,
            "name",
            default=loaded.code,
        )

        description = _read_frontmatter_value(
            loaded.skill.frontmatter,
            "description",
            default="No description provided.",
        )

        entries.append(
            "\n".join(
                [
                    f"- Skill name: `{name}`",
                    f"  Description: {description}",
                ]
            )
        )

    return "\n\n".join(entries)


def _read_frontmatter_value(
    frontmatter: Any,
    key: str,
    default: str,
) -> str:
    """
    Support either a model object or a dictionary-like frontmatter,
    depending on the installed ADK version.
    """

    if isinstance(frontmatter, dict):
        value = frontmatter.get(key, default)
    else:
        value = getattr(frontmatter, key, default)

    return str(value)
