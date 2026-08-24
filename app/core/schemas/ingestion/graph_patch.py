from typing import Any

from pydantic import BaseModel, ConfigDict, Field


TECHNICAL_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.-]*:[A-Za-z_][A-Za-z0-9_.-]*$"


class _DraftModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class Evidence(_DraftModel):
    source: str = Field(min_length=1)
    section: str | None = None
    text: str = Field(min_length=1)


class ExtractedProperty(_DraftModel):
    property_name: str = Field(
        alias="propertyName",
        pattern=TECHNICAL_NAME_PATTERN,
    )
    value: Any


class ExtractedNode(_DraftModel):
    temp_id: str = Field(alias="tempId", min_length=1)
    class_name: str = Field(
        alias="className",
        pattern=TECHNICAL_NAME_PATTERN,
    )
    properties: list[ExtractedProperty]
    evidence: list[Evidence] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractedEdge(_DraftModel):
    edge_name: str = Field(
        alias="edgeName",
        pattern=TECHNICAL_NAME_PATTERN,
    )
    source_temp_id: str = Field(alias="sourceTempId", min_length=1)
    target_temp_id: str = Field(alias="targetTempId", min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class GraphPatchDraft(_DraftModel):
    """Model-visible graph proposal produced by the semantic mapper."""

    nodes: list[ExtractedNode] = Field(min_length=1)
    edges: list[ExtractedEdge]
    warnings: list[str] = Field(default_factory=list)
