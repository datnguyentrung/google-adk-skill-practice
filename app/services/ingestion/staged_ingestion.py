from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import (
    ChunkCoverage,
    Evidence,
    ExtractedEdge,
    ExtractedNode,
    GraphPatchDraft,
    GraphPatchFragment,
)
from app.core.schemas.ingestion.workspace import (
    IngestionBatch,
    IngestionProvenance,
    IngestionWorkspace,
)

logger = logging.getLogger(__name__)

MAX_BATCH_CHUNKS = 5
MAX_BATCH_CHARS = 5_000


class WorkspaceConflictError(ValueError):
    """A retry would make the accumulated graph internally inconsistent."""

    def __init__(self, message: str, *, conflict: dict | None = None):
        super().__init__(message)
        self.conflict = conflict or {}


class IngestionWorkspaceService:
    """Own batch planning, idempotent replacement, and graph-fragment merging."""

    def begin(
        self,
        *,
        artifact_name: str,
        provenance: IngestionProvenance,
        chunks: list[DocumentChunk],
    ) -> IngestionWorkspace:
        batches = self._partition(chunks)
        logger.info(
            "INGESTION_DOCUMENT document=%s total_chunks=%s batch_count=%s",
            artifact_name,
            len(chunks),
            len(batches),
        )
        for batch in batches:
            logger.info(
                "EXTRACTION_BATCH batch=%s chunk_ids=%s input_chars=%s",
                batch.index,
                batch.chunk_indexes,
                batch.content_chars,
            )
        identity_material = provenance.identity_material(artifact_name)
        ingestion_id = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
        return IngestionWorkspace(
            ingestionId=ingestion_id,
            artifactName=artifact_name,
            provenance=provenance,
            chunks=chunks,
            batches=batches,
        )

    def submit(
        self,
        workspace: IngestionWorkspace,
        batch_index: int,
        fragment: GraphPatchFragment,
    ) -> IngestionWorkspace:
        if batch_index < 0 or batch_index >= len(workspace.batches):
            raise ValueError(f"Unknown batch index: {batch_index}")
        batch = workspace.batches[batch_index]
        self._validate_fragment_scope(batch, fragment)

        candidate = workspace.model_copy(deep=True)
        candidate.batches[batch_index].fragment = fragment
        fragments = [
            item.fragment
            for item in candidate.batches
            if item.fragment is not None
        ]
        self.merge_fragments(fragments)
        candidate.validated_fingerprint = None
        candidate.finalized_patch = None
        return candidate

    def merged_patch(self, workspace: IngestionWorkspace) -> GraphPatchDraft:
        fragments = [
            batch.fragment
            for batch in workspace.batches
            if batch.fragment is not None
        ]
        merged = self.merge_fragments(fragments)
        return GraphPatchDraft.model_validate(
            merged.model_dump(by_alias=True, mode="json")
        )

    @classmethod
    def merge_fragments(
        cls,
        fragments: list[GraphPatchFragment],
    ) -> GraphPatchFragment:
        """Merge typed fragments with the same conflict rules used by submission."""

        nodes = cls._merge_nodes(fragments)
        edges = cls._merge_edges(fragments)
        coverage = cls._merge_coverage(fragments)
        warnings = list(
            dict.fromkeys(
                warning
                for fragment in fragments
                for warning in fragment.warnings
            )
        )
        return GraphPatchFragment(
            nodes=nodes,
            edges=edges,
            coverage=coverage,
            warnings=warnings,
        )

    @staticmethod
    def next_batch(workspace: IngestionWorkspace) -> IngestionBatch | None:
        return next(
            (batch for batch in workspace.batches if batch.fragment is None),
            None,
        )

    @staticmethod
    def is_current(
        workspace: IngestionWorkspace,
        *,
        provenance: IngestionProvenance,
    ) -> bool:
        return workspace.provenance == provenance

    @staticmethod
    def _partition(chunks: list[DocumentChunk]) -> list[IngestionBatch]:
        if not chunks:
            raise ValueError("At least one source chunk is required")
        batches: list[IngestionBatch] = []
        current: list[DocumentChunk] = []
        current_chars = 0
        for chunk in chunks:
            chunk_chars = len(chunk.content)
            if chunk_chars > MAX_BATCH_CHARS:
                raise ValueError(
                    f"Chunk {chunk.index} exceeds {MAX_BATCH_CHARS} characters"
                )
            would_overflow = (
                len(current) >= MAX_BATCH_CHUNKS
                or current_chars + chunk_chars > MAX_BATCH_CHARS
            )
            if current and would_overflow:
                batches.append(
                    IngestionBatch(
                        index=len(batches),
                        chunkIndexes=[item.index for item in current],
                        contentChars=current_chars,
                    )
                )
                current = []
                current_chars = 0
            current.append(chunk)
            current_chars += chunk_chars
        if current:
            batches.append(
                IngestionBatch(
                    index=len(batches),
                    chunkIndexes=[item.index for item in current],
                    contentChars=current_chars,
                )
            )
        return batches

    @staticmethod
    def _validate_fragment_scope(
        batch: IngestionBatch,
        fragment: GraphPatchFragment,
    ) -> None:
        expected = set(batch.chunk_indexes)
        supplied = [item.chunk_index for item in fragment.coverage]
        if len(supplied) != len(set(supplied)) or set(supplied) != expected:
            raise WorkspaceConflictError(
                f"Batch {batch.index} coverage must contain exactly {sorted(expected)}"
            )
        for evidence in IngestionWorkspaceService._all_evidence(fragment):
            if evidence.chunk_index not in expected:
                raise WorkspaceConflictError(
                    f"Batch {batch.index} evidence references chunk "
                    f"{evidence.chunk_index} outside its scope"
                )

    @staticmethod
    def _all_evidence(fragment: GraphPatchFragment) -> Iterable[Evidence]:
        for node in fragment.nodes:
            yield from node.evidence
            for prop in node.properties:
                yield from prop.evidence
        for edge in fragment.edges:
            yield from edge.evidence

    @classmethod
    def _merge_nodes(
        cls,
        fragments: list[GraphPatchFragment],
    ) -> list[ExtractedNode]:
        merged: dict[str, ExtractedNode] = {}
        for fragment in fragments:
            for incoming in fragment.nodes:
                existing = merged.get(incoming.temp_id)
                if existing is None:
                    merged[incoming.temp_id] = incoming.model_copy(deep=True)
                    continue
                if existing.class_name != incoming.class_name:
                    raise WorkspaceConflictError(
                        f"Node {incoming.temp_id} changed class from "
                        f"{existing.class_name} to {incoming.class_name}"
                    )
                existing.evidence = cls._dedupe_models(
                    [*existing.evidence, *incoming.evidence]
                )
                existing.confidence = max(existing.confidence, incoming.confidence)
                properties = {item.property_name: item for item in existing.properties}
                for prop in incoming.properties:
                    current = properties.get(prop.property_name)
                    if current is None:
                        copied = prop.model_copy(deep=True)
                        existing.properties.append(copied)
                        properties[prop.property_name] = copied
                        continue
                    if isinstance(current.value, list) and isinstance(
                        prop.value,
                        list,
                    ):
                        current.value = cls._merge_list_values(
                            current.value,
                            prop.value,
                        )
                        current.evidence = cls._dedupe_models(
                            [*current.evidence, *prop.evidence]
                        )
                        continue
                    if cls._stable_value(current.value) != cls._stable_value(prop.value):
                        raise WorkspaceConflictError(
                            f"Node {incoming.temp_id} property {prop.property_name} "
                            "has conflicting values",
                            conflict={
                                "nodeTempId": incoming.temp_id,
                                "propertyName": prop.property_name,
                                "existingValue": current.value,
                                "incomingValue": prop.value,
                                "existingEvidence": [
                                    item.model_dump(
                                        by_alias=True,
                                        mode="json",
                                        exclude_none=True,
                                    )
                                    for item in current.evidence
                                ],
                                "incomingEvidence": [
                                    item.model_dump(
                                        by_alias=True,
                                        mode="json",
                                        exclude_none=True,
                                    )
                                    for item in prop.evidence
                                ],
                            },
                        )
                    current.evidence = cls._dedupe_models(
                        [*current.evidence, *prop.evidence]
                    )
        return list(merged.values())

    @classmethod
    def _merge_edges(
        cls,
        fragments: list[GraphPatchFragment],
    ) -> list[ExtractedEdge]:
        merged: dict[tuple[str, str, str], ExtractedEdge] = {}
        for fragment in fragments:
            for incoming in fragment.edges:
                key = (
                    incoming.edge_name,
                    incoming.source_temp_id,
                    incoming.target_temp_id,
                )
                existing = merged.get(key)
                if existing is None:
                    merged[key] = incoming.model_copy(deep=True)
                    continue
                existing.evidence = cls._dedupe_models(
                    [*existing.evidence, *incoming.evidence]
                )
                existing.confidence = max(existing.confidence, incoming.confidence)
        return list(merged.values())

    @staticmethod
    def _merge_coverage(
        fragments: list[GraphPatchFragment],
    ) -> list[ChunkCoverage]:
        merged: dict[int, ChunkCoverage] = {}
        for fragment in fragments:
            for incoming in fragment.coverage:
                existing = merged.get(incoming.chunk_index)
                if existing is not None and existing.decision != incoming.decision:
                    raise WorkspaceConflictError(
                        f"Chunk {incoming.chunk_index} has conflicting coverage decisions"
                    )
                merged[incoming.chunk_index] = incoming.model_copy(deep=True)
        return [merged[index] for index in sorted(merged)]

    @staticmethod
    def _stable_value(value) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @classmethod
    def _merge_list_values(cls, existing: list, incoming: list) -> list:
        merged = list(existing)
        seen = {cls._stable_value(item) for item in merged}
        for item in incoming:
            key = cls._stable_value(item)
            if key not in seen:
                merged.append(item)
                seen.add(key)
        return merged

    @staticmethod
    def _dedupe_models(items: list):
        unique = {}
        for item in items:
            key = json.dumps(
                item.model_dump(by_alias=True, mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            )
            unique[key] = item
        return list(unique.values())


__all__ = [
    "MAX_BATCH_CHARS",
    "MAX_BATCH_CHUNKS",
    "IngestionWorkspaceService",
    "WorkspaceConflictError",
]
