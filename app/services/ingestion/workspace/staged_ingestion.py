"""Phase 2b — Chia tài liệu thành batch và quản lý workspace ingestion theo phiên.

Tài liệu dài được chia thành nhiều batch theo số chunk/số ký tự/số token ước lượng.
Workspace giữ fragment của từng batch, gộp dần thành một graph patch duy nhất và
kiểm tra fragment có đúng phạm vi batch của nó hay không."""

import hashlib
import json
import logging
import math
import os
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
from app.services.ingestion.identity.policies import PRODUCT_SALES_NATURAL_KEYS

logger = logging.getLogger(__name__)


MAX_BATCH_CHUNKS = max(1, int(os.getenv("INGESTION_MAX_BATCH_CHUNKS", "5")))

TRUE_CHUNK_CACHE_MODE = os.getenv(
    "INGESTION_TRUE_CHUNK_CACHE", "false"
).strip().lower() in {"1", "true", "yes", "on"}


MAX_BATCH_CHARS = max(1_000, int(os.getenv("INGESTION_MAX_BATCH_CHARS", "5000")))


ESTIMATED_CHARS_PER_TOKEN = max(1.0, float(os.getenv("INGESTION_ESTIMATED_CHARS_PER_TOKEN", "2.0")))


MAX_BATCH_ESTIMATED_TOKENS = max(1_000, int(os.getenv("INGESTION_MAX_BATCH_ESTIMATED_TOKENS", "6000")))


class WorkspaceConflictError(ValueError):
    """
    Lỗi khi thao tác không khớp với workspace hiện tại (ví dụ batch đã submit).

    Args:
        message: Mô tả xung đột.
        conflict: Chi tiết xung đột kèm theo (tuỳ chọn).
    """

    def __init__(self, message: str, *, conflict: dict | None = None):
        """
        Ghi nhận message và chi tiết xung đột.

        Args:
            message: Mô tả xung đột.
            conflict: Dict chi tiết, mặc định là None.
        """
        super().__init__(message)
        self.conflict = conflict or {}


class IngestionWorkspaceService:
    """
    Quản lý vòng đời workspace ingestion: chia batch, nhận fragment, gộp patch.
    """

    def begin(
        self,
        *,
        artifact_name: str,
        provenance: IngestionProvenance,
        chunks: list[DocumentChunk],
    ) -> IngestionWorkspace:
        """
        Tạo workspace mới cho một tài liệu: chia batch và ghi provenance.

        Args:
            artifact_name: Tên tài liệu nguồn.
            provenance: Thông tin phiên bản nguồn/ontology/skill.
            chunks: Toàn bộ chunk của tài liệu.

        Returns:
            `IngestionWorkspace` đã chia batch.
        """
        batches = self._partition(chunks)
        identity_material = provenance.identity_material(artifact_name)
        ingestion_id = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
        logger.info(
            "INGESTION_DOCUMENT ingestion_id=%s document=%s total_chunks=%s batch_count=%s",
            ingestion_id,
            artifact_name,
            len(chunks),
            len(batches),
        )
        chunk_by_index = {chunk.index: chunk for chunk in chunks}
        for batch in batches:
            logger.info(
                "[INGESTION_BATCH_CREATED] ingestion_id=%s batch=%s chunk_ids=%s input_chars=%s",
                ingestion_id,
                batch.index,
                batch.chunk_indexes,
                batch.content_chars,
            )
            for chunk_index in batch.chunk_indexes:
                chunk = chunk_by_index[chunk_index]
                logger.debug(
                    "[INGESTION_BATCH_CREATED] ingestion_id=%s batch=%s chunk=%s "
                    "section=%r line_start=%s line_end=%s char_count=%s",
                    ingestion_id,
                    batch.index,
                    chunk.index,
                    chunk.section,
                    chunk.start_line,
                    chunk.end_line,
                    len(chunk.content),
                )
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
        """
        Nhận fragment của một batch và cập nhật workspace.

        Args:
            workspace: Workspace hiện tại.
            batch_index: Chỉ số batch đang submit.
            fragment: Fragment LLM trả về cho batch đó.

        Returns:
            Workspace sau khi ghi nhận fragment.

        Raises:
            WorkspaceConflictError: Batch không hợp lệ hoặc đã được submit.
        """
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
        """
        Gộp toàn bộ fragment đã nhận thành một `GraphPatchDraft` duy nhất.
        """
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
        """
        Gộp danh sách fragment thành một fragment (hợp nhất node, edge, coverage).
        """

        nodes, temp_id_aliases = cls._merge_nodes(fragments)
        edges = cls._merge_edges(fragments, temp_id_aliases)
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
        """
        Trả về batch kế tiếp chưa submit, hoặc `None` nếu đã hết.
        """
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
        """
        Kiểm tra workspace có còn khớp với provenance hiện tại (ontology/skill/artifact) không.
        """
        return workspace.provenance == provenance

    @staticmethod
    def _partition(chunks: list[DocumentChunk]) -> list[IngestionBatch]:
        """
        Chia chunk thành các batch theo giới hạn số lượng, ký tự và token ước lượng.
        """
        if not chunks:
            raise ValueError("At least one source chunk is required")
        batches: list[IngestionBatch] = []
        current: list[DocumentChunk] = []
        current_chars = 0
        current_tokens = 0
        max_batch_chunks = 1 if TRUE_CHUNK_CACHE_MODE else MAX_BATCH_CHUNKS
        for chunk in chunks:
            chunk_chars = len(chunk.content)
            chunk_tokens = max(1, math.ceil(chunk_chars / ESTIMATED_CHARS_PER_TOKEN))
            if chunk_chars > MAX_BATCH_CHARS or chunk_tokens > MAX_BATCH_ESTIMATED_TOKENS:
                raise ValueError(
                    f"Chunk {chunk.index} exceeds configured batch budget "
                    f"({chunk_chars} chars, ~{chunk_tokens} tokens)"
                )
            would_overflow = (
                len(current) >= max_batch_chunks
                or current_chars + chunk_chars > MAX_BATCH_CHARS
                or current_tokens + chunk_tokens > MAX_BATCH_ESTIMATED_TOKENS
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
                current_tokens = 0
            current.append(chunk)
            current_chars += chunk_chars
            current_tokens += chunk_tokens
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
        """
        Kiểm tra fragment chỉ tham chiếu tới chunk thuộc batch của nó.
        """
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
        """
        Liệt kê toàn bộ evidence của fragment (node, property, edge).
        """
        for node in fragment.nodes:
            yield from node.evidence
            for prop in node.properties:
                yield from prop.evidence
        for edge in fragment.edges:
            yield from edge.evidence

    @classmethod
    def _normalized_node_copy(cls, node: ExtractedNode) -> ExtractedNode:
        """
        Tạo bản copy node đã chuẩn hoá thuộc tính để gộp.
        """
        normalized = node.model_copy(deep=True)
        properties = {}
        ordered = []
        for prop in normalized.properties:
            current = properties.get(prop.property_name)
            if current is None:
                copied = prop.model_copy(deep=True)
                ordered.append(copied)
                properties[prop.property_name] = copied
                continue

            if isinstance(current.value, list) or isinstance(prop.value, list):
                current_values = current.value if isinstance(current.value, list) else [current.value]
                incoming_values = prop.value if isinstance(prop.value, list) else [prop.value]
                current.value = cls._merge_list_values(current_values, incoming_values)
                current.evidence = cls._dedupe_models([*current.evidence, *prop.evidence])
                continue

            if cls._stable_value(current.value) == cls._stable_value(prop.value):
                current.evidence = cls._dedupe_models([*current.evidence, *prop.evidence])
                continue

            raise WorkspaceConflictError(
                f"Node {node.temp_id} property {prop.property_name} "
                "has conflicting values inside one fragment",
                conflict={
                    "nodeTempId": node.temp_id,
                    "propertyName": prop.property_name,
                    "existingValue": current.value,
                    "incomingValue": prop.value,
                    "existingEvidence": [
                        item.model_dump(by_alias=True, mode="json", exclude_none=True)
                        for item in current.evidence
                    ],
                    "incomingEvidence": [
                        item.model_dump(by_alias=True, mode="json", exclude_none=True)
                        for item in prop.evidence
                    ],
                },
            )

        normalized.properties = ordered
        return normalized

    @classmethod
    def _merge_nodes(
        cls,
        fragments: list[GraphPatchFragment],
    ) -> tuple[list[ExtractedNode], dict[str, str]]:
        """
        Gộp node của nhiều fragment và trả về bảng ánh xạ tempId.
        """
        merged: dict[str, ExtractedNode] = {}
        temp_id_aliases: dict[str, str] = {}
        incoming_nodes = [
            cls._normalized_node_copy(node)
            for fragment in fragments
            for node in fragment.nodes
        ]
        # Canonical identities are merged first so later anonymous references can
        # resolve to the sole identified entity of the same class without guessing.
        incoming_nodes.sort(key=lambda node: cls._node_identity_key(node) is None)
        for incoming in incoming_nodes:
            original_temp_id = incoming.temp_id
            canonical_temp_id = cls._canonical_temp_id(incoming, merged)
            temp_id_aliases[original_temp_id] = canonical_temp_id
            if canonical_temp_id != incoming.temp_id:
                incoming.temp_id = canonical_temp_id
            existing = merged.get(canonical_temp_id)
            if existing is None:
                merged[canonical_temp_id] = incoming.model_copy(deep=True)
                continue
            if existing.class_name != incoming.class_name:
                raise WorkspaceConflictError(
                    f"Node {incoming.temp_id} changed class from "
                    f"{existing.class_name} to {incoming.class_name}"
                )
            existing.evidence = cls._dedupe_models([*existing.evidence, *incoming.evidence])
            existing.confidence = max(existing.confidence, incoming.confidence)
            properties = {item.property_name: item for item in existing.properties}
            for prop in incoming.properties:
                current = properties.get(prop.property_name)
                if current is None:
                    copied = prop.model_copy(deep=True)
                    existing.properties.append(copied)
                    properties[prop.property_name] = copied
                    continue
                if isinstance(current.value, list) or isinstance(prop.value, list):
                    current_values = current.value if isinstance(current.value, list) else [current.value]
                    incoming_values = prop.value if isinstance(prop.value, list) else [prop.value]
                    current.value = cls._merge_list_values(current_values, incoming_values)
                    current.evidence = cls._dedupe_models([*current.evidence, *prop.evidence])
                    continue
                if cls._stable_value(current.value) != cls._stable_value(prop.value):
                    raise WorkspaceConflictError(
                        f"Node {incoming.temp_id} property {prop.property_name} has conflicting values",
                        conflict={
                            "nodeTempId": incoming.temp_id,
                            "propertyName": prop.property_name,
                            "existingValue": current.value,
                            "incomingValue": prop.value,
                            "existingEvidence": [
                                item.model_dump(by_alias=True, mode="json", exclude_none=True)
                                for item in current.evidence
                            ],
                            "incomingEvidence": [
                                item.model_dump(by_alias=True, mode="json", exclude_none=True)
                                for item in prop.evidence
                            ],
                        },
                    )
                current.evidence = cls._dedupe_models([*current.evidence, *prop.evidence])
        return list(merged.values()), temp_id_aliases

    @classmethod
    def _merge_edges(
        cls,
        fragments: list[GraphPatchFragment],
        temp_id_aliases: dict[str, str] | None = None,
    ) -> list[ExtractedEdge]:
        """
        Gộp edge của nhiều fragment và cập nhật tempId theo bảng ánh xạ.
        """
        temp_id_aliases = temp_id_aliases or {}
        merged: dict[tuple[str, str, str], ExtractedEdge] = {}
        for fragment in fragments:
            for incoming in fragment.edges:
                incoming = incoming.model_copy(deep=True)
                incoming.source_temp_id = temp_id_aliases.get(
                    incoming.source_temp_id,
                    incoming.source_temp_id,
                )
                incoming.target_temp_id = temp_id_aliases.get(
                    incoming.target_temp_id,
                    incoming.target_temp_id,
                )
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

    @classmethod
    def _canonical_temp_id(
        cls,
        incoming: ExtractedNode,
        merged: dict[str, ExtractedNode],
    ) -> str:
        """
        Chọn tempId chuẩn cho node khi gộp (giữ bản đã xuất hiện).
        """
        identity_key = cls._node_identity_key(incoming)
        for existing in merged.values():
            if existing.class_name != incoming.class_name:
                continue
            if identity_key is not None and identity_key == cls._node_identity_key(existing):
                return existing.temp_id
        return incoming.temp_id

    @classmethod
    def _node_identity_key(cls, node: ExtractedNode) -> tuple[str, str, str] | None:
        """
        Tạo khoá nhận dạng node dùng khi gộp node giữa các fragment.
        """
        property_name = PRODUCT_SALES_NATURAL_KEYS.get(node.class_name)
        if property_name is None:
            return None
        properties = {item.property_name: item.value for item in node.properties}
        value = properties.get(property_name)
        if value is None:
            return None
        return node.class_name, property_name, cls._stable_value(value)

    @staticmethod
    def _merge_coverage(
        fragments: list[GraphPatchFragment],
    ) -> list[ChunkCoverage]:
        """
        Gộp coverage của nhiều fragment theo chunk index.
        """
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
        """
        Chuỗi hoá giá trị theo cách ổn định để so sánh khi gộp.
        """
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @classmethod
    def _merge_list_values(cls, existing: list, incoming: list) -> list:
        """
        Gộp hai danh sách giá trị và khử trùng.
        """
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
        """
        Khử trùng danh sách model theo khoá canonical.
        """
        unique = {}
        for item in items:
            key = json.dumps(
                item.model_dump(by_alias=True, mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            )
            unique[key] = item
        return list(unique.values())
