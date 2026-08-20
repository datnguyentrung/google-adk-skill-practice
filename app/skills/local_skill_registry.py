"""Lazy local registry for ADK skills."""

from __future__ import annotations

from google.adk.skills import SkillRegistry
from google.adk.skills import models

from app.skills.skill_loader import SkillDescriptor, load_skill_descriptor


class LocalSkillRegistry(SkillRegistry):
    """Load full local skills only when ADK requests them by name."""

    def __init__(self, descriptors: list[SkillDescriptor]):
        self._descriptors = {
            descriptor.name: descriptor
            for descriptor in descriptors
        }
        self._cache: dict[str, models.Skill] = {}

    async def get_skill(self, *, name: str) -> models.Skill:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        descriptor = self._descriptors.get(name)
        if descriptor is None:
            raise ValueError(f"Unknown local skill: {name}")

        skill = load_skill_descriptor(descriptor)
        self._cache[name] = skill
        return skill

    async def search_skills(self, *, query: str) -> list[models.Frontmatter]:
        normalized_query = query.strip().casefold()
        if not normalized_query:
            return [
                descriptor.frontmatter
                for descriptor in self._descriptors.values()
            ]

        terms = normalized_query.split()
        ranked: list[tuple[int, models.Frontmatter]] = []

        for descriptor in self._descriptors.values():
            haystack = (
                f"{descriptor.name} {descriptor.description}"
            ).casefold()
            score = sum(term in haystack for term in terms)
            if score:
                ranked.append((score, descriptor.frontmatter))
        ranked.sort(
            key=lambda item: (
                -item[0],
                item[1].name,
            )
        )
        return [frontmatter for _, frontmatter in ranked]

    def search_tool_description(self) -> str:
        return (
            "Searches the local skill catalog by terms in skill "
            "names and descriptions."
        )


__all__ = ["LocalSkillRegistry"]
