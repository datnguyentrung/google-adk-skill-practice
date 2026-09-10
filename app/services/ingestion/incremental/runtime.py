"""Pure helpers that bridge an ingestion workspace to incremental persistence."""

from __future__ import annotations

import json

from app.core.schemas.ingestion.source import ExtractionCacheEntry, SourceLifecycle
from app.core.schemas.ingestion.workspace import IngestionWorkspace
from app.services.ingestion.incremental.identity import effective_chunk_id


def lifecycle_from_workspace(workspace: IngestionWorkspace) -> SourceLifecycle:
    provenance = workspace.provenance
    return SourceLifecycle(
        documentId=provenance.document_id,
        documentName=workspace.artifact_name,
        contentHash=provenance.artifact_digest,
        configSignature=provenance.config_signature,
        ingestionSignature=provenance.ingestion_signature,
        versionId=provenance.source_version_id,
        ontologyDigest=provenance.ontology_digest,
        skillDigest=provenance.skill_digest,
        modelId=provenance.model_id,
        chunkerVersion=provenance.chunker_version,
        mapperVersion=provenance.mapper_version,
        compilerVersion=provenance.compiler_version,
    )


def cache_entries_from_workspace(
    workspace: IngestionWorkspace,
) -> list[ExtractionCacheEntry]:
    chunks_by_index = {chunk.index: chunk for chunk in workspace.chunks}
    entries: list[ExtractionCacheEntry] = []
    for batch in workspace.batches:
        if batch.fragment is None or not batch.extraction_cache_key:
            continue
        batch_chunks = [
            chunks_by_index[index]
            for index in batch.chunk_indexes
            if index in chunks_by_index
        ]
        entries.append(
            ExtractionCacheEntry(
                cacheKey=batch.extraction_cache_key,
                documentId=workspace.provenance.document_id,
                sourceVersionId=workspace.provenance.source_version_id,
                batchIndex=batch.index,
                chunkIds=[effective_chunk_id(chunk) for chunk in batch_chunks],
                graphContextDigest=batch.extraction_context_digest or "MISSING",
                fragmentJson=json.dumps(
                    batch.fragment.model_dump(
                        by_alias=True, mode="json", exclude_none=True
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
    return entries


__all__ = ["cache_entries_from_workspace", "lifecycle_from_workspace"]
