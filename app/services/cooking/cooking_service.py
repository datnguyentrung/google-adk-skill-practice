"""Validated, cached access to local cooking data."""

import json
import re
from pathlib import Path

from app.core.schemas.cooking import (
    Dish,
    DishListResponse,
    Recipe,
    RecipeResponse,
)
from app.services.text_search import normalize_text

_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?$"
)


class CookingService:
    """Load cooking fixtures once and provide deterministic domain operations."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or (
            Path(__file__).resolve().parents[2] / "data" / "cooking_data"
        )
        self._dishes: list[Dish] | None = None
        self._recipes: dict[str, RecipeResponse] | None = None

    @property
    def dishes(self) -> list[Dish]:
        """Return validated dish summaries loaded from every list fixture."""

        if self._dishes is None:
            dishes_by_id: dict[str, Dish] = {}
            for path in sorted(self._data_dir.glob("list_*.json")):
                response = DishListResponse.model_validate(self._read_json(path))
                dishes_by_id.update({dish.id: dish for dish in response.data})
            self._dishes = list(dishes_by_id.values())
        return self._dishes

    @property
    def recipes(self) -> dict[str, RecipeResponse]:
        """Return validated recipe responses indexed by dish ID."""

        if self._recipes is None:
            recipes: dict[str, RecipeResponse] = {}
            for path in sorted(self._data_dir.glob("*_recipe.json")):
                response = RecipeResponse.model_validate(self._read_json(path))
                recipes[response.data.id] = response
            self._recipes = recipes
        return self._recipes

    def search(
        self,
        query: str | None,
        ingredients: list[str],
        category: str | None,
        cuisine: str | None,
        dietary: list[str],
        difficulty: str | None,
        tags: list[str],
        maximum_total_time: int | None,
        top_k: int,
    ) -> list[Dish]:
        """Filter dish metadata and rank remaining dishes by textual relevance."""

        normalized_query = normalize_text(query or "")
        normalized_ingredients = self._normalized_values(ingredients)
        text_terms = [normalized_query, *normalized_ingredients]
        text_terms = [term for term in text_terms if term]

        ranked: list[tuple[float, str, str, Dish]] = []
        for dish in self.dishes:
            if not self._matches_filters(
                dish,
                category,
                cuisine,
                dietary,
                difficulty,
                tags,
                maximum_total_time,
            ):
                continue

            score = self._relevance(dish, text_terms)
            if text_terms and score == 0:
                continue
            ranked.append((score, normalize_text(dish.name), dish.id, dish))

        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        return [item[3] for item in ranked[:top_k]]

    def get_recipe(self, dish_id: str) -> RecipeResponse | None:
        """Return the full recipe for a dish when local detail data exists."""

        return self.recipes.get(dish_id)

    @staticmethod
    def scale(
        recipe: Recipe,
        desired_servings: int,
    ) -> tuple[float, list[dict[str, object]]]:
        """Return scaled ingredient groups without mutating the recipe."""

        scale_factor = desired_servings / recipe.meta.yield_count
        groups: list[dict[str, object]] = []
        for group in recipe.ingredients:
            group_data = group.model_dump(mode="json")
            for item in group_data["items"]:
                item["quantity"] = round(item["quantity"] * scale_factor, 6)
            groups.append(group_data)
        return scale_factor, groups

    def _matches_filters(
        self,
        dish: Dish,
        category: str | None,
        cuisine: str | None,
        dietary: list[str],
        difficulty: str | None,
        tags: list[str],
        maximum_total_time: int | None,
    ) -> bool:
        exact_filters = (
            (category, dish.category),
            (cuisine, dish.cuisine),
            (difficulty, dish.difficulty),
        )
        if any(
            requested and normalize_text(requested) != normalize_text(actual)
            for requested, actual in exact_filters
        ):
            return False

        dish_dietary = set(self._normalized_values(dish.dietary.flags))
        if not set(self._normalized_values(dietary)) <= dish_dietary:
            return False

        dish_tags = set(self._normalized_values(dish.tags))
        if not set(self._normalized_values(tags)) <= dish_tags:
            return False

        return (
            maximum_total_time is None
            or self._duration_minutes(dish.meta.total_time) <= maximum_total_time
        )

    @staticmethod
    def _relevance(dish: Dish, terms: list[str]) -> float:
        if not terms:
            return 0.0

        name_tokens = set(normalize_text(dish.name).split())
        corpus_tokens = set(
            normalize_text(" ".join([dish.name, dish.description, *dish.tags])).split()
        )
        score = 0.0
        for term in terms:
            tokens = set(term.split())
            if not tokens:
                continue
            score += len(tokens & corpus_tokens) / len(tokens)
            score += len(tokens & name_tokens) / len(tokens)
        return score

    @staticmethod
    def _normalized_values(values: list[str]) -> list[str]:
        return [normalized for value in values if (normalized := normalize_text(value))]

    @staticmethod
    def _duration_minutes(value: str) -> int:
        match = _DURATION_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError(f"Unsupported ISO-8601 duration: {value}")
        parts = {key: int(number or 0) for key, number in match.groupdict().items()}
        return parts["days"] * 1440 + parts["hours"] * 60 + parts["minutes"]

    @staticmethod
    def _read_json(path: Path) -> object:
        with path.open(encoding="utf-8") as data_file:
            return json.load(data_file)


__all__ = ["CookingService"]
