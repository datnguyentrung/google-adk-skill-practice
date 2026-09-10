"""Deterministic identities for source versions and extraction cache entries."""

from __future__ import annotations

import hashlib
import json

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.source import SourceLifecycle

INGESTION_SIGNATURE_VERSION = "ingestion-signature-v1"
CACHE_KEY_VERSION = "batch-cache-v1"


def _digest_payload(payload: dict) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_config_signature(
    *, ontology_digest: str, skill_digest: str, model_id: str,
    chunker_version: str, mapper_version: str, compiler_version: str,
) -> str:
    return _digest_payload(
        {
            "version": INGESTION_SIGNATURE_VERSION,
            "ontologyDigest": ontology_digest,
            "skillDigest": skill_digest,
            "modelId": model_id,
            "chunkerVersion": chunker_version,
            "mapperVersion": mapper_version,
            "compilerVersion": compiler_version,
        }
    )


def build_ingestion_signature(
    *, document_id: str, content_hash: str, config_signature: str
) -> str:
    return _digest_payload(
        {
            "version": INGESTION_SIGNATURE_VERSION,
            "documentId": document_id,
            "contentHash": content_hash,
            "configSignature": config_signature,
        }
    )


def build_source_version_id(document_id: str, ingestion_signature: str) -> str:
    digest = _digest_payload(
        {"documentId": document_id, "ingestionSignature": ingestion_signature}
    )
    return f"srcv_{digest[:40]}"


def effective_chunk_id(chunk: DocumentChunk) -> str:
    if chunk.chunk_id:
        return chunk.chunk_id
    fallback = _digest_payload(
        {
            "source": chunk.source,
            "index": chunk.index,
            "section": chunk.section,
            "content": chunk.content,
        }
    )
    return f"legacy_chk_{fallback[:40]}"


def build_batch_cache_key(
    *, lifecycle: SourceLifecycle, chunks: list[DocumentChunk], graph_context: str
) -> tuple[str, str]:
    graph_context_digest = hashlib.sha256(
        graph_context.encode("utf-8")
    ).hexdigest()
    cache_key = _digest_payload(
        {
            "version": CACHE_KEY_VERSION,
            "documentId": lifecycle.document_id,
            "configSignature": lifecycle.config_signature,
            "chunkIds": [effective_chunk_id(chunk) for chunk in chunks],
            "graphContextDigest": graph_context_digest,
        }
    )
    return f"cache_{cache_key}", graph_context_digest
