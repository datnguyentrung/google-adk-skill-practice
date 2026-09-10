from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Mapping

from app.scripts.preprocessing.normalizer import (
    CanonicalFact,
    normalize_for_comparison,
    parse_attribute_line,
)

_HEADING_RE = re.compile(r"^(#{1,6})(?:\s+(.+))?$")


class ValidationSeverity(StrEnum):
    ERROR = "error"

    WARNING = "warning"


class ExpectedDatatype(StrEnum):
    STRING = "string"

    BOOLEAN = "boolean"

    INTEGER = "integer"

    DECIMAL = "decimal"

    DATE = "date"

    MONEY = "money"

    PERCENTAGE = "percentage"

    DURATION = "duration"


@dataclass(frozen=True)
class FieldRule:
    datatype: ExpectedDatatype | None = None

    required: bool = False

    min_value: Decimal | int | float | str | None = None

    max_value: Decimal | int | float | str | None = None

    allowed_values: tuple[str, ...] | None = None

    def min_decimal(
        self,
    ) -> Decimal | None:

        if self.min_value is None:
            return None

        return Decimal(str(self.min_value))

    def max_decimal(
        self,
    ) -> Decimal | None:

        if self.max_value is None:
            return None

        return Decimal(str(self.max_value))


@dataclass(frozen=True)
class ValidationIssue:
    code: str

    message: str

    severity: ValidationSeverity

    line: int | None = None

    section: str | None = None

    attribute: str | None = None


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(
        self,
    ) -> list[ValidationIssue]:

        return [
            issue
            for issue in self.issues
            if (issue.severity == ValidationSeverity.ERROR)
        ]

    @property
    def warnings(
        self,
    ) -> list[ValidationIssue]:

        return [
            issue
            for issue in self.issues
            if (issue.severity == ValidationSeverity.WARNING)
        ]

    @property
    def is_valid(
        self,
    ) -> bool:

        return not self.errors

    def add(
        self,
        *,
        code: str,
        message: str,
        severity: ValidationSeverity,
        line: int | None = None,
        section: str | None = None,
        attribute: str | None = None,
    ) -> None:

        self.issues.append(
            ValidationIssue(
                code=code,
                message=message,
                severity=severity,
                line=line,
                section=section,
                attribute=attribute,
            )
        )


@dataclass(frozen=True)
class FactOccurrence:
    fact: CanonicalFact

    line: int

    section: str


def _is_fence_line(
    line: str,
) -> bool:

    stripped = line.lstrip()

    return stripped.startswith("```") or stripped.startswith("~~~")


def collect_facts(
    text: str,
    attribute_aliases: Mapping[str, str] | None = None,
) -> list[FactOccurrence]:

    facts: list[FactOccurrence] = []

    current_section = "<document>"

    inside_fence = False
    fence_token: str | None = None

    for line_number, line in enumerate(
        text.splitlines(),
        start=1,
    ):
        stripped_left = line.lstrip()

        if _is_fence_line(line):
            token = stripped_left[:3]

            if not inside_fence:
                inside_fence = True
                fence_token = token

            elif token == fence_token:
                inside_fence = False
                fence_token = None

            continue

        if inside_fence:
            continue

        heading = _HEADING_RE.match(line.strip())

        if heading:
            title = (heading.group(2) or "").strip()

            current_section = title or f"H{len(heading.group(1))}"

            continue

        fact = parse_attribute_line(
            line,
            attribute_aliases,
        )

        if fact is not None:
            facts.append(
                FactOccurrence(
                    fact=fact,
                    line=line_number,
                    section=current_section,
                )
            )

    return facts


def markdown_validator(
    text: str,
    result: ValidationResult,
) -> None:

    if not text.strip():
        result.add(
            code="EMPTY_DOCUMENT",
            message=("Markdown document is empty."),
            severity=ValidationSeverity.ERROR,
        )

        return

    inside_fence = False
    fence_token: str | None = None

    for line in text.splitlines():
        stripped_left = line.lstrip()

        if _is_fence_line(line):
            token = stripped_left[:3]

            if not inside_fence:
                inside_fence = True
                fence_token = token

            elif token == fence_token:
                inside_fence = False
                fence_token = None

    if inside_fence:
        result.add(
            code="UNCLOSED_CODE_FENCE",
            message=("Markdown contains an unclosed fenced code block."),
            severity=ValidationSeverity.ERROR,
        )


def structure_validator(
    text: str,
    result: ValidationResult,
) -> None:

    previous_level: int | None = None

    heading_count = 0

    inside_fence = False
    fence_token: str | None = None

    for line_number, line in enumerate(
        text.splitlines(),
        start=1,
    ):
        stripped_left = line.lstrip()

        if _is_fence_line(line):
            token = stripped_left[:3]

            if not inside_fence:
                inside_fence = True
                fence_token = token

            elif token == fence_token:
                inside_fence = False
                fence_token = None

            continue

        if inside_fence:
            continue

        match = _HEADING_RE.match(line.strip())

        if not match:
            continue

        heading_count += 1

        level = len(match.group(1))

        title = (match.group(2) or "").strip()

        if not title:
            result.add(
                code="EMPTY_HEADING",
                message=(f"H{level} heading has no title."),
                severity=ValidationSeverity.WARNING,
                line=line_number,
            )

        if previous_level is not None and level > previous_level + 1:
            result.add(
                code="HEADING_LEVEL_JUMP",
                message=(f"Heading jumps from H{previous_level} to H{level}."),
                severity=ValidationSeverity.WARNING,
                line=line_number,
                section=title or None,
            )

        previous_level = level

    if heading_count == 0:
        result.add(
            code="NO_HEADINGS",
            message=("Document has no Markdown headings."),
            severity=ValidationSeverity.WARNING,
        )

    _validate_table_shapes(
        text,
        result,
    )


def _table_cells(
    line: str,
) -> list[str] | None:

    stripped = line.strip()

    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None

    if r"\|" in stripped:
        return None

    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _validate_table_shapes(
    text: str,
    result: ValidationResult,
) -> None:

    expected_columns: int | None = None

    in_table = False

    inside_fence = False
    fence_token: str | None = None

    for line_number, line in enumerate(
        text.splitlines(),
        start=1,
    ):
        stripped_left = line.lstrip()

        if _is_fence_line(line):
            token = stripped_left[:3]

            if not inside_fence:
                inside_fence = True
                fence_token = token

            elif token == fence_token:
                inside_fence = False
                fence_token = None

            continue

        if inside_fence:
            continue

        cells = _table_cells(line)

        if cells is None:
            in_table = False
            expected_columns = None

            continue

        if not in_table:
            in_table = True
            expected_columns = len(cells)

            continue

        if expected_columns is not None and len(cells) != expected_columns:
            result.add(
                code="TABLE_COLUMN_MISMATCH",
                message=(
                    f"Table row has {len(cells)} columns; expected {expected_columns}."
                ),
                severity=ValidationSeverity.ERROR,
                line=line_number,
            )


def _datatype_matches(
    occurrence: FactOccurrence,
    expected: ExpectedDatatype,
) -> bool:

    value = occurrence.fact.value

    if expected == ExpectedDatatype.STRING:
        return True

    if expected == ExpectedDatatype.BOOLEAN:
        return value.kind == "boolean"

    if expected == ExpectedDatatype.DATE:
        return value.kind == "date"

    if expected == ExpectedDatatype.MONEY:
        return value.kind == "money"

    if expected == ExpectedDatatype.PERCENTAGE:
        return value.kind == "percentage"

    if expected == ExpectedDatatype.DURATION:
        return value.kind == "duration"

    if expected == ExpectedDatatype.DECIMAL:
        return value.kind == "number" and value.number is not None

    if expected == ExpectedDatatype.INTEGER:
        return (
            value.kind == "number"
            and value.number is not None
            and value.number == value.number.to_integral()
        )

    return False


def datatype_validator(
    facts: list[FactOccurrence],
    field_rules: Mapping[str, FieldRule],
    result: ValidationResult,
) -> None:

    for occurrence in facts:
        rule = field_rules.get(occurrence.fact.attribute)

        if rule is None or rule.datatype is None:
            continue

        if _datatype_matches(
            occurrence,
            rule.datatype,
        ):
            continue

        result.add(
            code="DATATYPE_MISMATCH",
            message=(
                f"Expected "
                f"{rule.datatype.value}, "
                f"got "
                f"{occurrence.fact.value.kind}: "
                f"{occurrence.fact.raw_value!r}."
            ),
            severity=ValidationSeverity.ERROR,
            line=occurrence.line,
            section=occurrence.section,
            attribute=(occurrence.fact.attribute),
        )


def range_validator(
    facts: list[FactOccurrence],
    field_rules: Mapping[str, FieldRule],
    result: ValidationResult,
) -> None:

    for occurrence in facts:
        rule = field_rules.get(occurrence.fact.attribute)

        if rule is None:
            continue

        number = occurrence.fact.value.number

        if number is None:
            continue

        min_value = rule.min_decimal()
        max_value = rule.max_decimal()

        if min_value is not None and number < min_value:
            result.add(
                code="VALUE_BELOW_MIN",
                message=(f"Value {number} is below minimum {min_value}."),
                severity=ValidationSeverity.ERROR,
                line=occurrence.line,
                section=occurrence.section,
                attribute=(occurrence.fact.attribute),
            )

        if max_value is not None and number > max_value:
            result.add(
                code="VALUE_ABOVE_MAX",
                message=(f"Value {number} is above maximum {max_value}."),
                severity=ValidationSeverity.ERROR,
                line=occurrence.line,
                section=occurrence.section,
                attribute=(occurrence.fact.attribute),
            )


def required_field_validator(
    facts: list[FactOccurrence],
    field_rules: Mapping[str, FieldRule],
    result: ValidationResult,
) -> None:

    present = {occurrence.fact.attribute for occurrence in facts}

    for attribute, rule in field_rules.items():
        if rule.required and attribute not in present:
            result.add(
                code="REQUIRED_FIELD_MISSING",
                message=(f"Required field {attribute!r} is missing."),
                severity=ValidationSeverity.ERROR,
                attribute=attribute,
            )


def allowed_value_validator(
    facts: list[FactOccurrence],
    field_rules: Mapping[str, FieldRule],
    result: ValidationResult,
) -> None:

    for occurrence in facts:
        rule = field_rules.get(occurrence.fact.attribute)

        if rule is None or not rule.allowed_values:
            continue

        normalized_allowed = {
            normalize_for_comparison(value) for value in rule.allowed_values
        }

        observed = normalize_for_comparison(occurrence.fact.value.normalized)

        if observed in normalized_allowed:
            continue

        result.add(
            code="VALUE_NOT_ALLOWED",
            message=(
                f"Value "
                f"{occurrence.fact.value.normalized!r} "
                f"is not one of "
                f"{rule.allowed_values!r}."
            ),
            severity=ValidationSeverity.ERROR,
            line=occurrence.line,
            section=occurrence.section,
            attribute=(occurrence.fact.attribute),
        )


def conflict_detector(
    facts: list[FactOccurrence],
    result: ValidationResult,
    *,
    severity: ValidationSeverity = (ValidationSeverity.WARNING),
) -> None:

    seen: dict[
        tuple[str, str],
        tuple[str, int],
    ] = {}

    for occurrence in facts:
        key = (
            occurrence.section,
            occurrence.fact.attribute,
        )

        value = occurrence.fact.value.normalized

        previous = seen.get(key)

        if previous is None:
            seen[key] = (
                value,
                occurrence.line,
            )

            continue

        previous_value, previous_line = previous

        if previous_value == value:
            continue

        result.add(
            code="ATTRIBUTE_VALUE_CONFLICT",
            message=(
                f"Conflicting values for "
                f"{occurrence.fact.attribute!r}: "
                f"{previous_value!r} "
                f"at line {previous_line} "
                f"vs "
                f"{value!r} "
                f"at line {occurrence.line}."
            ),
            severity=severity,
            line=occurrence.line,
            section=occurrence.section,
            attribute=(occurrence.fact.attribute),
        )


def validate_markdown(
    text: str,
    *,
    attribute_aliases: Mapping[str, str] | None = None,
    field_rules: Mapping[str, FieldRule] | None = None,
    conflict_severity: ValidationSeverity = (ValidationSeverity.WARNING),
) -> ValidationResult:

    result = ValidationResult()

    markdown_validator(
        text,
        result,
    )

    if not text.strip():
        return result

    structure_validator(
        text,
        result,
    )

    facts = collect_facts(
        text,
        attribute_aliases,
    )

    rules = field_rules or {}

    if rules:
        datatype_validator(
            facts,
            rules,
            result,
        )

        range_validator(
            facts,
            rules,
            result,
        )

        required_field_validator(
            facts,
            rules,
            result,
        )

        allowed_value_validator(
            facts,
            rules,
            result,
        )

    conflict_detector(
        facts,
        result,
        severity=conflict_severity,
    )

    return result
