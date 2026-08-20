from typing import Any

from pydantic import BaseModel, Field


## Tóm tắt ontology
class OntologySummary(BaseModel):
    source_files: int = Field(alias="sourceFiles")  # Số lượng file nguồn
    classes: int  # Số lượng lớp (types)
    edges: int  # Số lượng cạnh (relationships)
    attributes: int  # Số lượng thuộc tính (properties)


## Quy tắc ontology
class OntologyRule(BaseModel):
    property: str  # Tên thuộc tính
    operator: str  # Toán tử
    value: Any  # Giá trị
    qualifier: str | None = None  # Bộ định tính


## Lớp ontology
class OntologyClass(BaseModel):
    name: str  # Tên lớp
    technical_name: str = Field(alias="technicalName")  # Tên kỹ thuật
    local_name: str = Field(alias="localName")  # Tên cục bộ
    iri: str  # Chỉ số tài nguyên quốc tế
    label: str  # Nhãn
    definition: str  # Định nghĩa

    parents: list[str] = []  # Các lớp cha
    rules: list[OntologyRule] = []  # Các quy tắc


## Cạnh ontology
class OntologyEdge(BaseModel):
    kind: str  # Loại cạnh
    name: str  # Tên cạnh
    technical_name: str = Field(alias="technicalName")  # Tên kỹ thuật
    local_name: str = Field(alias="localName")  # Tên cục bộ
    iri: str  # Chỉ số tài nguyên quốc tế
    label: str  # Nhãn
    definition: str  # Định nghĩa

    domain: list[str]  # Lớp nguồn
    range: list[str]  # Lớp đích


## Thuộc tính ontology
class OntologyAttribute(BaseModel):
    kind: str  # Loại thuộc tính

    name: str  # Tên thuộc tính
    technical_name: str = Field(alias="technicalName")  # Tên kỹ thuật
    local_name: str = Field(alias="localName")  # Tên cục bộ
    iri: str  # Chỉ số tài nguyên quốc tế
    label: str  # Nhãn
    definition: str  # Định nghĩa

    domain: list[str]  # Lớp nguồn
    range: list[str]  # Lớp đích


## Định nghĩa ontology
class OntologyDefinition(BaseModel):
    source_dir: str = Field(alias="sourceDir")  # Thư mục nguồn
    source_files: list[str] = Field(alias="sourceFiles")  # Danh sách file nguồn

    summary: OntologySummary  # Tóm tắt ontology

    classes: list[OntologyClass]  # Danh sách lớp
    edges: list[OntologyEdge]  # Danh sách cạnh
    attributes: list[OntologyAttribute]  # Danh sách thuộc tính
