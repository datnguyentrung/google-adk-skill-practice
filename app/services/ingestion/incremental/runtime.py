"""Pure helpers that bridge an ingestion workspace to incremental persistence."""

from app.core.schemas.ingestion.source import SourceLifecycle
from app.core.schemas.ingestion.workspace import IngestionWorkspace


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


__all__ = ["lifecycle_from_workspace"]
