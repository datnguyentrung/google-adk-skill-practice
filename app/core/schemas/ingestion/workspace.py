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

    def identity_material(self, artifact_name: str) -> str:
        return (
            f"{artifact_name}\0{self.artifact_digest}\0"
            f"{self.ontology_digest}\0{self.skill_digest}"
        )


class IngestionRetryState(_WorkspaceModel):
    batch_index: int = Field(alias="batchIndex", ge=0)
    coverage_not_evidenced_chunk_indexes: list[int] = Field(
        default_factory=list,
        alias="coverageNotEvidencedChunkIndexes",
    )
    fragment_fingerprint: str = Field(alias="fragmentFingerprint", min_length=1)
    error_codes: list[str] = Field(default_factory=list, alias="errorCodes")


class IngestionWorkspace(_WorkspaceModel):
    ingestion_id: str = Field(alias="ingestionId", min_length=1)
    artifact_name: str = Field(alias="artifactName", min_length=1)
    provenance: IngestionProvenance
    chunks: list[DocumentChunk] = Field(min_length=1)
    batches: list[IngestionBatch] = Field(min_length=1)
    validated_fingerprint: str | None = Field(
        default=None,
        alias="validatedFingerprint",
    )
    finalized_patch: dict | None = Field(default=None, alias="finalizedPatch")
    retry_states: dict[str, IngestionRetryState] = Field(
        default_factory=dict,
        alias="retryStates",
    )
    skipped_chunk_indexes: list[int] = Field(
        default_factory=list, alias="skippedChunkIndexes"
    )
    ingestion_warnings: list[dict[str, Any]] = Field(
        default_factory=list, alias="ingestionWarnings"
    )

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
    "IngestionRetryState",
    "IngestionWorkspace",
]
