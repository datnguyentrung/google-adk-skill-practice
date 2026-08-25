from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class XsdDatatype(StrEnum):
    STRING = "string"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    DATETIME = "datetime"


_XSD_KIND_BY_RANGE = {
    "xsd:string": XsdDatatype.STRING,
    "xsd:anyURI": XsdDatatype.STRING,
    "xsd:boolean": XsdDatatype.BOOLEAN,
    "xsd:integer": XsdDatatype.INTEGER,
    "xsd:decimal": XsdDatatype.DECIMAL,
    "xsd:date": XsdDatatype.DATE,
    "xsd:dateTime": XsdDatatype.DATETIME,
}


def xsd_datatypes(ranges: list[str]) -> tuple[XsdDatatype, ...]:
    return tuple(
        dict.fromkeys(
            _XSD_KIND_BY_RANGE[item]
            for item in ranges
            if item in _XSD_KIND_BY_RANGE
        )
    )


def value_matches_xsd(value: Any, datatype: XsdDatatype) -> bool:
    if datatype is XsdDatatype.STRING:
        return isinstance(value, str)
    if datatype is XsdDatatype.BOOLEAN:
        return isinstance(value, bool)
    if datatype is XsdDatatype.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if datatype is XsdDatatype.DECIMAL:
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    if datatype is XsdDatatype.DATE:
        return _is_iso_date(value)
    if datatype is XsdDatatype.DATETIME:
        return _is_iso_datetime(value)
    return False


def _is_iso_date(value: Any) -> bool:
    if isinstance(value, datetime):
        return False
    if isinstance(value, date):
        return True
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
        return len(value) == 10
    except ValueError:
        return False


def _is_iso_datetime(value: Any) -> bool:
    if isinstance(value, datetime):
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


__all__ = ["XsdDatatype", "value_matches_xsd", "xsd_datatypes"]
