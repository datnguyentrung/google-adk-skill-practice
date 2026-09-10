from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SourceVersionStatus(StrEnum):
    PENDING = "PENDING"
    WRITTEN = "WRITTEN"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    DELETED = "DELETED"


class _SourceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class SourceLifecycle(_SourceModel):
    document_id: str = Field(alias="documentId", min_length=1)
    document_name: str = Field(alias="documentName", min_length=1)
    content_hash: str = Field(alias="contentHash", min_length=1)
    config_signature: str = Field(alias="configSignature", min_length=1)
    ingestion_signature: str = Field(alias="ingestionSignature", min_length=1)
    version_id: str = Field(alias="versionId", min_length=1)
    ontology_digest: str = Field(alias="ontologyDigest", min_length=1)
    skill_digest: str = Field(alias="skillDigest", min_length=1)
    model_id: str = Field(alias="modelId", min_length=1)
    chunker_version: str = Field(alias="chunkerVersion", min_length=1)
    mapper_version: str = Field(alias="mapperVersion", min_length=1)
    compiler_version: str = Field(alias="compilerVersion", min_length=1)


class ExtractionCacheEntry(_SourceModel):
    cache_key: str = Field(alias="cacheKey", min_length=1)
    document_id: str = Field(alias="documentId", min_length=1)
    source_version_id: str = Field(alias="sourceVersionId", min_length=1)
    batch_index: int = Field(alias="batchIndex", ge=0)
    chunk_ids: list[str] = Field(alias="chunkIds", min_length=1)
    graph_context_digest: str = Field(alias="graphContextDigest", min_length=1)
    fragment_json: str = Field(alias="fragmentJson", min_length=2)


__all__ = [
    "ExtractionCacheEntry",
    "SourceLifecycle",
    "SourceVersionStatus",
]
