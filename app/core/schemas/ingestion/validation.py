from pydantic import BaseModel, ConfigDict, Field


class ValidationIssue(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    code: str
    message: str
    location: str
    node_temp_id: str | None = Field(default=None, alias="nodeTempId")
    property_name: str | None = Field(default=None, alias="propertyName")
    edge_name: str | None = Field(default=None, alias="edgeName")


class GraphPatchValidationResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )

    valid_for_extraction: bool = Field(alias="validForExtraction")
    valid_for_persistence: bool = Field(alias="validForPersistence")
    errors: list[ValidationIssue]
    readiness_issues: list[ValidationIssue] = Field(alias="readinessIssues")
    warnings: list[ValidationIssue]
    node_count: int = Field(alias="nodeCount", ge=0)
    edge_count: int = Field(alias="edgeCount", ge=0)
