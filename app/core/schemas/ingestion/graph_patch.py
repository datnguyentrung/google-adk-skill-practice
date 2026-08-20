from typing import Any

from pydantic import BaseModel, Field


## Chứng cứ
class Evidence(BaseModel):
    source: str
    section: str | None = None
    text: str


## Nút trích xuất
class ExtractedNode(BaseModel):
    temp_id: str = Field(alias="tempId")  # Tạm thời
    class_name: str = Field(alias="className")  # Lớp
    properties: dict[str, Any]  # Thuộc tính

    evidence: list[Evidence]  # Chứng cứ
    confidence: float = Field(ge=0.0, le=1.0)  # Độ tin cậy


## Cạnh trích xuất
class ExtractedEdge(BaseModel):
    edge_name: str = Field(alias="edgeName")  # Tên cạnh
    source_temp_id: str = Field(alias="sourceTempId")  # Nút tạm thời bắt đầu
    target_temp_id: str = Field(alias="targetTempId")  # Nút tạm thời kết thúc

    evidence: list[Evidence]  # Chứng cứ
    confidence: float = Field(ge=0.0, le=1.0)  # Độ tin cậy


## Bản vá đồ thị
class GraphPatch(BaseModel):
    nodes: list[ExtractedNode]  # Danh sách các nút được trích xuất
    edges: list[ExtractedEdge]  # Danh sách các cạnh được trích xuất
    warnings: list[str] = Field(default_factory=list)  # Danh sách các cảnh báo
