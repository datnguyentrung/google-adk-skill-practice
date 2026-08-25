from __future__ import annotations

from typing import Any

from app.core.schemas.ingestion.validation import ValidationIssue


def deduplicate_issues(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    unique: dict[tuple[str, str, str], ValidationIssue] = {}
    for issue in issues:
        unique[(str(issue.code), issue.location, issue.message)] = issue
    return list(unique.values())


def cardinality_failure(
    operator: str,
    raw_expected: Any,
    actual_count: int,
) -> tuple[str, int] | None:
    if operator == "some":
        return ("minimum", 1) if actual_count < 1 else None
    try:
        expected = int(raw_expected)
    except (TypeError, ValueError):
        return None
    if operator == "exactlyQualified" and actual_count != expected:
        return ("exactly", expected)
    if operator == "minQualified" and actual_count < expected:
        return ("minimum", expected)
    return None


__all__ = ["cardinality_failure", "deduplicate_issues"]
