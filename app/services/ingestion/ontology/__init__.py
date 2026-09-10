"""Phase 0 — Nạp và tra cứu ontology.

Package này cung cấp định nghĩa ontology đã được kiểm tra (`OntologyLoader`), chỉ
mục tra cứu kèm quy tắc suy diễn (`OntologyRegistry`) và các tiện ích kiểm tra kiểu
dữ liệu XSD (`XsdDatatype`).

Caller bên ngoài nên import từ package này thay vì import thẳng vào module bên trong.
"""

from app.services.ingestion.ontology.datatypes import (
    XsdDatatype,
    normalize_xsd_value,
    value_matches_xsd,
    xsd_datatypes,
)
from app.services.ingestion.ontology.loader import OntologyLoader
from app.services.ingestion.ontology.registry import OntologyRegistry

__all__ = [
    "OntologyLoader",
    "OntologyRegistry",
    "XsdDatatype",
    "normalize_xsd_value",
    "value_matches_xsd",
    "xsd_datatypes",
]
