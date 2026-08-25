from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ValidationCode(StrEnum):
    SCHEMA_INVALID = "SCHEMA_INVALID"
    TECHNICAL_NAME_INVALID = "TECHNICAL_NAME_INVALID"
    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    DUPLICATE_TEMP_ID = "DUPLICATE_TEMP_ID"
    DUPLICATE_PROPERTY = "DUPLICATE_PROPERTY"
    DANGLING_REFERENCE = "DANGLING_REFERENCE"
    SEMANTIC_CONFLICT = "SEMANTIC_CONFLICT"
    DUPLICATE_COVERAGE = "DUPLICATE_COVERAGE"
    COVERAGE_MISSING = "COVERAGE_MISSING"
    COVERAGE_UNKNOWN_CHUNK = "COVERAGE_UNKNOWN_CHUNK"
    COVERAGE_NOT_EVIDENCED = "COVERAGE_NOT_EVIDENCED"
    COVERAGE_CONFLICT = "COVERAGE_CONFLICT"
    EVIDENCE_UNKNOWN_CHUNK = "EVIDENCE_UNKNOWN_CHUNK"
    EVIDENCE_SOURCE_MISMATCH = "EVIDENCE_SOURCE_MISMATCH"
    EVIDENCE_SECTION_MISMATCH = "EVIDENCE_SECTION_MISMATCH"
    EVIDENCE_TEXT_NOT_IN_SOURCE = "EVIDENCE_TEXT_NOT_IN_SOURCE"
    PROPERTY_VALUE_NOT_GROUNDED = "PROPERTY_VALUE_NOT_GROUNDED"
    EDGE_RELATION_NOT_GROUNDED = "EDGE_RELATION_NOT_GROUNDED"
    DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE = (
        "DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE"
    )
    UNKNOWN_CLASS = "UNKNOWN_CLASS"
    UNKNOWN_PROPERTY = "UNKNOWN_PROPERTY"
    PROPERTY_DOMAIN_MISMATCH = "PROPERTY_DOMAIN_MISMATCH"
    PROPERTY_DATATYPE_MISMATCH = "PROPERTY_DATATYPE_MISMATCH"
    UNKNOWN_EDGE = "UNKNOWN_EDGE"
    EDGE_DOMAIN_MISMATCH = "EDGE_DOMAIN_MISMATCH"
    EDGE_RANGE_MISMATCH = "EDGE_RANGE_MISMATCH"
    ONTOLOGY_RULE_UNSATISFIED = "ONTOLOGY_RULE_UNSATISFIED"
    SOURCE_WARNING = "SOURCE_WARNING"
    SOURCE_CONTEXT_REQUIRED = "SOURCE_CONTEXT_REQUIRED"
    IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
    VALIDATION_PRECONDITION = "VALIDATION_PRECONDITION"
    NEO4J_WRITE_FAILED = "NEO4J_WRITE_FAILED"
    WORKSPACE_PRECONDITION = "WORKSPACE_PRECONDITION"
    BATCH_CONFLICT = "BATCH_CONFLICT"
    BATCH_INCOMPLETE = "BATCH_INCOMPLETE"
    UNCHANGED_RETRY = "UNCHANGED_RETRY"
    ORCHESTRATION_FAILED = "ORCHESTRATION_FAILED"
    READBACK_MISMATCH = "READBACK_MISMATCH"
    RECEIPT_ARTIFACT_FAILED = "RECEIPT_ARTIFACT_FAILED"


class ValidationIssue(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    code: ValidationCode
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
