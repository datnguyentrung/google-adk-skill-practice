from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchFragment


class _WorkspaceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class IngestionBatch(_WorkspaceModel):
    index: int = Field(ge=0)
    chunk_indexes: list[int] = Field(alias="chunkIndexes", min_length=1)
    content_chars: int = Field(alias="contentChars", ge=0)
    status: str = Field(default="PENDING")
    node_count: int = Field(default=0, alias="nodeCount", ge=0)
    edge_count: int = Field(default=0, alias="edgeCount", ge=0)
    coverage_count: int = Field(default=0, alias="coverageCount", ge=0)
    staged_at: str | None = Field(default=None, alias="stagedAt")
    retry_count: int = Field(default=0, alias="retryCount", ge=0)
    fragment: GraphPatchFragment | None = None


class IngestionProvenance(_WorkspaceModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        frozen=True,
    )

    artifact_digest: str = Field(alias="artifactDigest", min_length=1)
    ontology_digest: str = Field(alias="ontologyDigest", min_length=1)
    skill_digest: str = Field(alias="skillDigest", min_length=1)
    document_id: str = Field(default="MISSING", alias="documentId", min_length=1)
    config_signature: str = Field(
        default="MISSING", alias="configSignature", min_length=1
    )
    ingestion_signature: str = Field(
        default="MISSING", alias="ingestionSignature", min_length=1
    )
    source_version_id: str = Field(
        default="MISSING", alias="sourceVersionId", min_length=1
    )
    model_id: str = Field(default="MISSING", alias="modelId", min_length=1)
    chunker_version: str = Field(
        default="MISSING", alias="chunkerVersion", min_length=1
    )
    mapper_version: str = Field(
        default="MISSING", alias="mapperVersion", min_length=1
    )
    compiler_version: str = Field(
        default="MISSING", alias="compilerVersion", min_length=1
    )

    def identity_material(self, artifact_name: str) -> str:
        if self.ingestion_signature != "MISSING":
            return f"{self.document_id}\0{self.ingestion_signature}"
        return (
            f"{artifact_name}\0{self.artifact_digest}\0"
            f"{self.ontology_digest}\0{self.skill_digest}"
        )


class IngestionWorkspace(_WorkspaceModel):
    ingestion_id: str = Field(alias="ingestionId", min_length=1)
    artifact_name: str = Field(alias="artifactName", min_length=1)
    provenance: IngestionProvenance
    chunks: list[DocumentChunk] = Field(min_length=1)
    batches: list[IngestionBatch] = Field(min_length=1)
    status: str = Field(default="PROCESSING")
    staged_node_count: int = Field(default=0, alias="stagedNodeCount", ge=0)
    staged_edge_count: int = Field(default=0, alias="stagedEdgeCount", ge=0)
    conflict_count: int = Field(default=0, alias="conflictCount", ge=0)
    pending_edge_count: int = Field(default=0, alias="pendingEdgeCount", ge=0)
    validated_fingerprint: str | None = Field(
        default=None, alias="validatedFingerprint"
    )
    skipped_chunk_indexes: list[int] = Field(
        default_factory=list, alias="skippedChunkIndexes"
    )
    ingestion_warnings: list[dict[str, Any]] = Field(
        default_factory=list, alias="ingestionWarnings"
    )
    staging_schema_version: int = Field(default=2, alias="stagingSchemaVersion", ge=1)
    staging_revision: int = Field(default=0, alias="stagingRevision", ge=0)
    last_finalized_revision: int = Field(
        default=-1, alias="lastFinalizedRevision", ge=-1
    )
    last_readiness_fingerprint: str | None = Field(
        default=None, alias="lastReadinessFingerprint"
    )
    last_readiness_issues: list[dict[str, Any]] = Field(
        default_factory=list, alias="lastReadinessIssues"
    )
    repair_batch_indexes: list[int] = Field(
        default_factory=list, alias="repairBatchIndexes"
    )
    repair_attempts_by_batch: dict[int, int] = Field(
        default_factory=dict, alias="repairAttemptsByBatch"
    )
    validation_attempts_by_batch: dict[int, int] = Field(
        default_factory=dict, alias="validationAttemptsByBatch"
    )
    terminal_error_code: str | None = Field(default=None, alias="terminalErrorCode")

    @property
    def artifact_digest(self) -> str:
        return self.provenance.artifact_digest

    @property
    def ontology_digest(self) -> str:
        return self.provenance.ontology_digest

    @property
    def skill_digest(self) -> str:
        return self.provenance.skill_digest


__all__ = [
    "IngestionBatch",
    "IngestionProvenance",
    "IngestionWorkspace",
]
