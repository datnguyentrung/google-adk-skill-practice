from __future__ import annotations

import re
import unicodedata

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Mapping


_SPACE_RE = re.compile(r"\s+")

_BULLET_ATTRIBUTE_RE = re.compile(
    r"^(?P<indent>\s*)-\s+"
    r"(?P<attribute>[^:\n]{1,120})"
    r"\s*:\s*"
    r"(?P<value>.+?)\s*$"
)

_PLAIN_ATTRIBUTE_RE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<attribute>[^:\n]{1,120})"
    r"\s*:\s*"
    r"(?P<value>.+?)\s*$"
)


_BOOLEAN_TRUE = {
    "true",
    "yes",
    "y",
    "có",
    "co",
    "đúng",
    "dung",
}

_BOOLEAN_FALSE = {
    "false",
    "no",
    "n",
    "không",
    "khong",
    "sai",
}


_PERCENT_RE = re.compile(
    r"^\s*([+-]?[\d\s.,]+)\s*"
    r"(?:%|phần\s*trăm)\s*$",
    re.IGNORECASE,
)


_DURATION_RE = re.compile(
    r"^\s*([+-]?[\d\s.,]+)\s*"
    r"(ngày|day|days|d|"
    r"tháng|month|months|"
    r"năm|year|years|yr|yrs)"
    r"\s*$",
    re.IGNORECASE,
)


_MONEY_RE = re.compile(
    r"^\s*([+-]?[\d\s.,]+)\s*"
    r"(k|nghìn|ngan|ngàn|"
    r"triệu|trieu|tr|m|"
    r"tỷ|ty|b)?\s*"
    r"(vnd|vnđ|đồng|dong)?"
    r"\s*$",
    re.IGNORECASE,
)


_MULTIPLIERS: dict[str, Decimal] = {
    "k": Decimal("1000"),
    "nghìn": Decimal("1000"),
    "ngan": Decimal("1000"),
    "ngàn": Decimal("1000"),

    "triệu": Decimal("1000000"),
    "trieu": Decimal("1000000"),
    "tr": Decimal("1000000"),
    "m": Decimal("1000000"),

    "tỷ": Decimal("1000000000"),
    "ty": Decimal("1000000000"),
    "b": Decimal("1000000000"),
}


_DURATION_UNITS = {
    "ngày": "day",
    "day": "day",
    "days": "day",
    "d": "day",

    "tháng": "month",
    "month": "month",
    "months": "month",

    "năm": "year",
    "year": "year",
    "years": "year",
    "yr": "year",
    "yrs": "year",
}


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
)


@dataclass(frozen=True)
class CanonicalValue:

    raw: str

    normalized: str

    kind: str

    number: Decimal | None = None

    unit: str | None = None


@dataclass(frozen=True)
class CanonicalFact:

    attribute: str

    value: CanonicalValue

    raw_attribute: str

    raw_value: str

    line: str

    @property
    def key(self) -> str:
        return (
            f"{self.attribute}|"
            f"{self.value.normalized}"
        )


def normalize_for_comparison(
    value: str,
) -> str:

    value = unicodedata.normalize(
        "NFKC",
        value,
    )

    value = value.strip().casefold()

    return _SPACE_RE.sub(
        " ",
        value,
    )


def _normalize_alias_map(
    aliases: Mapping[str, str] | None,
) -> dict[str, str]:

    if not aliases:
        return {}

    return {
        normalize_for_comparison(key): canonical
        for key, canonical in aliases.items()
    }


def normalize_attribute_name(
    attribute: str,
    aliases: Mapping[str, str] | None = None,
) -> str:

    key = normalize_for_comparison(
        attribute
    )

    alias_map = _normalize_alias_map(
        aliases
    )

    return alias_map.get(
        key,
        key,
    )


def _decimal_to_text(
    number: Decimal,
) -> str:

    if number == number.to_integral():
        return str(
            number.to_integral()
        )

    return format(
        number.normalize(),
        "f",
    )


def _parse_localized_decimal(
    raw: str,
) -> Decimal | None:

    value = (
        raw
        .replace("\u00a0", " ")
        .replace(" ", "")
        .strip()
    )

    if not value:
        return None

    sign = ""

    if value[0] in "+-":
        sign = value[0]
        value = value[1:]

    if not value:
        return None

    if not re.fullmatch(
        r"[\d.,]+",
        value,
    ):
        return None

    comma_count = value.count(",")
    dot_count = value.count(".")

    def groups_are_thousands(
        parts: list[str],
    ) -> bool:

        return (
            len(parts) > 1
            and 1 <= len(parts[0]) <= 3
            and all(
                len(part) == 3
                for part in parts[1:]
            )
        )

    if comma_count and dot_count:

        last_comma = value.rfind(",")
        last_dot = value.rfind(".")

        decimal_sep = (
            ","
            if last_comma > last_dot
            else "."
        )

        thousands_sep = (
            "."
            if decimal_sep == ","
            else ","
        )

        tail = value.split(
            decimal_sep
        )[-1]

        if len(tail) in (1, 2):

            value = value.replace(
                thousands_sep,
                "",
            )

            value = value.replace(
                decimal_sep,
                ".",
            )

        else:
            value = (
                value
                .replace(",", "")
                .replace(".", "")
            )

    elif comma_count:

        parts = value.split(",")

        if groups_are_thousands(parts):
            value = "".join(parts)

        elif comma_count == 1:
            value = value.replace(
                ",",
                ".",
            )

        else:
            return None

    elif dot_count:

        parts = value.split(".")

        if groups_are_thousands(parts):
            value = "".join(parts)

        elif dot_count == 1:

            if (
                len(parts[1]) == 3
                and 1 <= len(parts[0]) <= 3
            ):
                value = "".join(parts)

        else:
            return None

    try:
        return Decimal(
            sign + value
        )

    except InvalidOperation:
        return None


def _try_parse_date(
    raw: str,
) -> CanonicalValue | None:

    value = raw.strip()

    for fmt in _DATE_FORMATS:

        try:
            parsed = datetime.strptime(
                value,
                fmt,
            ).date()

            return CanonicalValue(
                raw=raw,
                normalized=parsed.isoformat(),
                kind="date",
            )

        except ValueError:
            continue

    return None


def _try_parse_boolean(
    raw: str,
) -> CanonicalValue | None:

    key = normalize_for_comparison(
        raw
    )

    if key in _BOOLEAN_TRUE:

        return CanonicalValue(
            raw=raw,
            normalized="true",
            kind="boolean",
        )

    if key in _BOOLEAN_FALSE:

        return CanonicalValue(
            raw=raw,
            normalized="false",
            kind="boolean",
        )

    return None


def _try_parse_percentage(
    raw: str,
) -> CanonicalValue | None:

    match = _PERCENT_RE.match(
        raw
    )

    if not match:
        return None

    number = _parse_localized_decimal(
        match.group(1)
    )

    if number is None:
        return None

    return CanonicalValue(
        raw=raw,
        normalized=(
            f"{_decimal_to_text(number)} %"
        ),
        kind="percentage",
        number=number,
        unit="%",
    )


def _try_parse_duration(
    raw: str,
) -> CanonicalValue | None:

    match = _DURATION_RE.match(
        raw
    )

    if not match:
        return None

    number = _parse_localized_decimal(
        match.group(1)
    )

    if number is None:
        return None

    unit_key = normalize_for_comparison(
        match.group(2)
    )

    unit = _DURATION_UNITS[
        unit_key
    ]

    return CanonicalValue(
        raw=raw,
        normalized=(
            f"{_decimal_to_text(number)} "
            f"{unit}"
        ),
        kind="duration",
        number=number,
        unit=unit,
    )


def _try_parse_money(
    raw: str,
) -> CanonicalValue | None:

    match = _MONEY_RE.match(
        raw
    )

    if not match:
        return None

    (
        numeric_raw,
        multiplier_raw,
        currency_raw,
    ) = match.groups()

    # Plain "500" không tự động coi là money.
    if (
        not multiplier_raw
        and not currency_raw
    ):
        return None

    number = _parse_localized_decimal(
        numeric_raw
    )

    if number is None:
        return None

    if multiplier_raw:

        multiplier_key = (
            normalize_for_comparison(
                multiplier_raw
            )
        )

        multiplier = _MULTIPLIERS.get(
            multiplier_key
        )

        if multiplier is None:
            return None

        number *= multiplier

    return CanonicalValue(
        raw=raw,
        normalized=(
            f"{_decimal_to_text(number)} VND"
        ),
        kind="money",
        number=number,
        unit="VND",
    )


def _try_parse_number(
    raw: str,
) -> CanonicalValue | None:

    if not re.fullmatch(
        r"\s*[+-]?[\d\s.,]+\s*",
        raw,
    ):
        return None

    number = _parse_localized_decimal(
        raw
    )

    if number is None:
        return None

    return CanonicalValue(
        raw=raw,
        normalized=_decimal_to_text(
            number
        ),
        kind="number",
        number=number,
    )


def normalize_value(
    raw: str,
) -> CanonicalValue:

    raw = unicodedata.normalize(
        "NFKC",
        raw,
    ).strip()

    raw = _SPACE_RE.sub(
        " ",
        raw,
    )

    parsers = (
        _try_parse_date,
        _try_parse_boolean,
        _try_parse_percentage,
        _try_parse_duration,
        _try_parse_money,
        _try_parse_number,
    )

    for parser in parsers:

        parsed = parser(raw)

        if parsed is not None:
            return parsed

    return CanonicalValue(
        raw=raw,
        normalized=(
            normalize_for_comparison(raw)
        ),
        kind="text",
    )


def parse_attribute_line(
    line: str,
    aliases: Mapping[str, str] | None = None,
) -> CanonicalFact | None:

    match = _BULLET_ATTRIBUTE_RE.match(
        line
    )

    is_bullet = match is not None

    if match is None:

        match = _PLAIN_ATTRIBUTE_RE.match(
            line
        )

        if match is None:
            return None

    raw_attribute = (
        match
        .group("attribute")
        .strip()
    )

    raw_value = (
        match
        .group("value")
        .strip()
    )

    alias_map = _normalize_alias_map(
        aliases
    )

    canonical_names = {
        normalize_for_comparison(value)
        for value in alias_map.values()
    }

    attribute_key = (
        normalize_for_comparison(
            raw_attribute
        )
    )

    # Dòng thường "X: Y" chỉ parse nếu X nằm trong
    # config attribute.
    if not is_bullet and aliases:

        if (
            attribute_key not in alias_map
            and attribute_key not in canonical_names
        ):
            return None

    elif (
        not is_bullet
        and not aliases
    ):
        return None

    attribute = normalize_attribute_name(
        raw_attribute,
        aliases,
    )

    value = normalize_value(
        raw_value
    )

    return CanonicalFact(
        attribute=attribute,
        value=value,
        raw_attribute=raw_attribute,
        raw_value=raw_value,
        line=line,
    )


def normalize_attribute_lines(
    text: str,
    aliases: Mapping[str, str] | None = None,
    *,
    rewrite_values: bool = True,
) -> str:

    result: list[str] = []

    inside_fence = False
    fence_token: str | None = None

    for line in text.splitlines():

        stripped = line.lstrip()

        is_fence = (
            stripped.startswith("```")
            or stripped.startswith("~~~")
        )

        if is_fence:

            token = stripped[:3]

            result.append(line)

            if not inside_fence:
                inside_fence = True
                fence_token = token

            elif token == fence_token:
                inside_fence = False
                fence_token = None

            continue

        if inside_fence:
            result.append(line)
            continue

        fact = parse_attribute_line(
            line,
            aliases,
        )

        if fact is None:
            result.append(line)
            continue

        bullet_match = (
            _BULLET_ATTRIBUTE_RE.match(line)
        )

        match = (
            bullet_match
            or _PLAIN_ATTRIBUTE_RE.match(line)
        )

        assert match is not None

        indent = match.group(
            "indent"
        )

        prefix = (
            "- "
            if bullet_match
            else ""
        )

        value = (
            fact.value.normalized
            if rewrite_values
            else fact.raw_value
        )

        result.append(
            f"{indent}"
            f"{prefix}"
            f"{fact.attribute}: "
            f"{value}"
        )

    return "\n".join(result)


def normalize_markdown(
    text: str,
    attribute_aliases: Mapping[str, str] | None = None,
    *,
    rewrite_values: bool = True,
) -> str:

    return normalize_attribute_lines(
        text,
        aliases=attribute_aliases,
        rewrite_values=rewrite_values,
    )
