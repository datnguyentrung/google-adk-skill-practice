"""Phase 0 — Kiểm tra và chuẩn hoá kiểu dữ liệu XSD của giá trị ontology.

Ontology khai báo `range` của từng property bằng các kiểu XSD (xsd:string,
xsd:date, xsd:decimal...). Module này quy đổi các range đó về `XsdDatatype` và
cung cấp hai phép kiểm tra: giá trị có đúng kiểu không, và giá trị đã được
chuẩn hoá chưa."""

import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class XsdDatatype(StrEnum):
    """
    Tập kiểu XSD mà ingestion hỗ trợ kiểm tra.
    """
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
    """
    Quy đổi danh sách range XSD thành các `XsdDatatype` (đã khử trùng, giữ thứ tự).
    """
    return tuple(
        dict.fromkeys(
            _XSD_KIND_BY_RANGE[item] for item in ranges if item in _XSD_KIND_BY_RANGE
        )
    )


def normalize_xsd_value(value: Any, datatype: XsdDatatype) -> Any:
    """
    Chuẩn hoá giá trị theo kiểu XSD (ví dụ chuỗi → date/decimal) để so sánh nhất quán.
    """
    if datatype is not XsdDatatype.DATE or not isinstance(value, str):
        return value
    raw = value.strip()
    if _is_iso_date(raw):
        return raw
    match = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", raw)
    if match is None:
        return value
    day, month, year = map(int, match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return value


def value_matches_xsd(value: Any, datatype: XsdDatatype) -> bool:
    """
    Kiểm tra một giá trị có đúng kiểu XSD yêu cầu hay không.
    """
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
    """
    Kiểm tra chuỗi có phải ngày ISO 8601 (YYYY-MM-DD) hợp lệ.
    """
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
    """
    Kiểm tra chuỗi có phải datetime ISO 8601 hợp lệ.
    """
    if isinstance(value, datetime):
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False
