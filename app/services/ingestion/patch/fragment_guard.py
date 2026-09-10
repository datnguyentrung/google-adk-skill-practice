"""Phase 3 — Chốt chặn (guard) cho từng fragment trước khi gộp vào workspace.

Guard chỉ chạy các kiểm tra TẤT ĐỊNH: evidence có nằm trong chunk nguồn, fragment
có đúng schema, class/property/edge có tồn tại và giá trị có đúng datatype. Nhờ đó
lỗi cơ bản bị chặn ngay tại batch thay vì đợi đến bước validate toàn cục."""

from typing import Any
from pydantic import ValidationError
from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import (
    Evidence,
    GraphPatchDraft,
    GraphPatchFragment,
)

from app.services.ingestion.ontology.registry import OntologyRegistry
from app.services.ingestion.patch.compiler import GraphPatchCompiler
from app.services.ingestion.patch.excerpts import (
    _all_evidence,
    _canonical_bullet_excerpt,
    _canonical_markdown_excerpt,
    _canonical_table_excerpt,
    _canonical_whitespace_excerpt,
)
from app.services.ingestion.validation.ontology_validator import (
    OntologyValidator,
    is_source_required_rule,
)
from app.services.ingestion.validation.source_grounding import SourceGroundingValidator


class GraphFragmentGuard:
    """
    Deterministic checks only: evidence, schema, ontology and datatype.
    """

    def __init__(self, *, registry: OntologyRegistry, compiler: GraphPatchCompiler, ontology_validator: OntologyValidator):
        """
        Ghi nhận registry, compiler và ontology validator dùng cho guard.

        Args:
            registry: Registry ontology.
            compiler: Compiler dùng để thử compile fragment.
            ontology_validator: Validator kiểm tra class/property/edge.
        """
        self.registry = registry
        self.compiler = compiler
        self.ontology_validator = ontology_validator

    def canonicalize(
            self, fragment: GraphPatchFragment, chunks: list[DocumentChunk]
        ) -> GraphPatchFragment:
            """
            Chuẩn hoá fragment: evidence khớp nguyên văn với chunk và tempId được chuẩn hoá.

            Args:
                fragment: Fragment LLM trả về cho một batch.
                chunks: Chunk nguồn của batch.

            Returns:
                Fragment đã canonical hoá evidence.
            """
            chunk_by_index = {chunk.index: chunk for chunk in chunks}

            def normalize(items: list[Evidence]) -> list[Evidence]:
                result = []
                for item in items:
                    chunk = chunk_by_index.get(item.chunk_index)
                    if chunk is None:
                        result.append(item)
                        continue
                    text = _canonical_table_excerpt(chunk, item.text)
                    text = _canonical_whitespace_excerpt(chunk, text)
                    text = _canonical_markdown_excerpt(chunk, text)
                    text = _canonical_bullet_excerpt(chunk, text)
                    result.append(
                        item.model_copy(
                            update={
                                "source": chunk.source,
                                "section": chunk.section,
                                "text": text,
                            }
                        )
                    )
                return result

            nodes = []
            for node in fragment.nodes:
                default_properties = {
                    attribute.technical_name
                    for attribute, _ in self.registry.configured_defaults_for_class(
                        node.class_name
                    )
                }
                properties = [
                    prop.model_copy(update={"evidence": normalize(prop.evidence)})
                    for prop in node.properties
                    if not (
                        self.registry.is_runtime_managed_attribute(prop.property_name)
                        or self.registry.edge_names_deriving_property(prop.property_name)
                        or prop.property_name in default_properties
                    )
                ]
                nodes.append(
                    node.model_copy(
                        update={
                            "properties": properties,
                            "evidence": normalize(node.evidence),
                        }
                    )
                )
            edges = [
                edge.model_copy(update={"evidence": normalize(edge.evidence)})
                for edge in fragment.edges
            ]
            cited_chunks = (
                {evidence.chunk_index for node in nodes for evidence in node.evidence}
                | {
                    evidence.chunk_index
                    for node in nodes
                    for prop in node.properties
                    for evidence in prop.evidence
                }
                | {evidence.chunk_index for edge in edges for evidence in edge.evidence}
            )
            coverage = []
            for item in fragment.coverage:
                if item.chunk_index in cited_chunks and item.decision != "MAPPED":
                    item = item.model_copy(
                        update={
                            "decision": "MAPPED",
                            "reason": "Graph evidence emitted for this chunk",
                        }
                    )
                elif item.chunk_index not in cited_chunks and item.decision == "MAPPED":
                    item = item.model_copy(
                        update={
                            "decision": "AMBIGUOUS",
                            "reason": "Mapper marked MAPPED but emitted no graph evidence for this chunk",
                        }
                    )
                coverage.append(item)
            return fragment.model_copy(
                update={"nodes": nodes, "edges": edges, "coverage": coverage}
            )

    def validate(
            self,
            fragment: GraphPatchFragment,
            chunks: list[DocumentChunk],
            *,
            existing_node_refs: set[str] | None = None,
        ) -> list[dict[str, Any]]:
            """
            Trả về danh sách lỗi tất định của fragment (rỗng nghĩa là fragment hợp lệ).

            Args:
                fragment: Fragment cần kiểm tra.
                chunks: Chunk nguồn của batch.
                existing_node_refs: tempId đã có từ các batch trước (nếu có).

            Returns:
                Danh sách dict lỗi theo format lỗi của ingestion.
            """
            errors: list[dict[str, Any]] = []
            chunk_by_index = {chunk.index: chunk for chunk in chunks}
            expected = set(chunk_by_index)
            supplied = [item.chunk_index for item in fragment.coverage]
            if len(supplied) != len(set(supplied)) or set(supplied) != expected:
                errors.append(
                    {
                        "code": "BATCH_COVERAGE_MISMATCH",
                        "message": f"coverage must contain exactly {sorted(expected)}",
                    }
                )
            for location, evidence in _all_evidence(fragment):
                chunk = chunk_by_index.get(evidence.chunk_index)
                if chunk is None:
                    errors.append(
                        {
                            "code": "EVIDENCE_OUTSIDE_BATCH",
                            "location": location,
                            "message": f"unknown chunk {evidence.chunk_index}",
                        }
                    )
                elif evidence.text not in chunk.content and evidence.text not in (
                    chunk.section or ""
                ):
                    errors.append(
                        {
                            "code": "EVIDENCE_NOT_VERBATIM",
                            "location": location,
                            "message": "evidence.text is not an exact substring of the cited chunk",
                        }
                    )
            for node_index, node in enumerate(fragment.nodes):
                for property_index, prop in enumerate(node.properties):
                    attribute = self.registry.get_attribute(prop.property_name)
                    if (
                        attribute is not None
                        and attribute.ingestion_policy.grounding == "source_literal"
                        and not SourceGroundingValidator._value_supported_chunks(
                            prop.value, prop.evidence, attribute.range
                        )
                    ):
                        errors.append(
                            {
                                "code": "SOURCE_LITERAL_NOT_GROUNDED",
                                "location": f"nodes.{node_index}.properties.{property_index}",
                                "propertyName": prop.property_name,
                                "value": prop.value,
                                "range": attribute.range,
                                "evidence": [item.text for item in prop.evidence[:3]],
                                "message": (
                                    f"{prop.property_name} is source_literal but its value "
                                    "is not a literal/datatype match for the cited evidence. "
                                    "For xsd:string, copy the property value verbatim from "
                                    "one cited evidence.text or omit the optional property."
                                ),
                            }
                        )

            fact_chunks = (
                {
                    evidence.chunk_index
                    for node in fragment.nodes
                    for evidence in node.evidence
                }
                | {
                    evidence.chunk_index
                    for node in fragment.nodes
                    for prop in node.properties
                    for evidence in prop.evidence
                }
                | {
                    evidence.chunk_index
                    for edge in fragment.edges
                    for evidence in edge.evidence
                }
            )
            for item in fragment.coverage:
                if item.decision == "MAPPED" and item.chunk_index not in fact_chunks:
                    errors.append(
                        {
                            "code": "MAPPED_CHUNK_WITHOUT_FACT",
                            "location": f"coverage.{item.chunk_index}",
                            "message": (
                                f"Chunk {item.chunk_index} is MAPPED but no node, property, "
                                "or edge cites that chunk"
                            ),
                        }
                    )
                if item.decision != "MAPPED" and item.chunk_index in fact_chunks:
                    errors.append(
                        {
                            "code": "COVERAGE_CONFLICT",
                            "location": f"coverage.{item.chunk_index}",
                            "message": (
                                f"Chunk {item.chunk_index} is {item.decision} but graph "
                                "facts cite that chunk"
                            ),
                        }
                    )

            existing_refs = existing_node_refs or set()
            errors.extend(
                self._required_source_property_errors(
                    fragment,
                    existing_node_refs=existing_refs,
                )
            )
            errors.extend(
                self._required_derived_edge_errors(
                    fragment,
                    existing_node_refs=existing_refs,
                )
            )

            if not fragment.nodes:
                if fragment.edges:
                    errors.append(
                        {"code": "EDGE_WITHOUT_NODES", "message": "edges require nodes"}
                    )
                if any(item.decision == "MAPPED" for item in fragment.coverage):
                    errors.append(
                        {
                            "code": "MAPPED_WITHOUT_GRAPH",
                            "message": "MAPPED coverage requires graph content",
                        }
                    )
                return errors
            try:
                draft = GraphPatchDraft.model_validate(
                    fragment.model_dump(by_alias=True, mode="json")
                )
            except ValidationError as exc:
                errors.append(
                    {
                        "code": "GRAPH_DRAFT_INVALID",
                        "message": str(exc),
                    }
                )
                return errors
            compiled = self.compiler.compile(draft)
            if compiled.compiled_patch is None:
                errors.extend(
                    issue.model_dump(by_alias=True, exclude_none=True)
                    for issue in compiled.errors
                )
                return errors
            errors.extend(
                issue.model_dump(by_alias=True, exclude_none=True)
                for issue in self.ontology_validator.validate_extraction(
                    compiled.compiled_patch
                )
            )
            return errors

    def _required_source_property_errors(
        self,
        fragment: GraphPatchFragment,
        *,
        existing_node_refs: set[str],
    ) -> list[dict[str, Any]]:
        """
        Báo lỗi khi fragment thiếu thuộc tính bắt buộc phải có trong nguồn tài liệu.
        """
        errors: list[dict[str, Any]] = []
        for node_index, node in enumerate(fragment.nodes):
            if node.temp_id in existing_node_refs:
                continue
            ontology_class = self.registry.get_class(node.class_name)
            if ontology_class is None:
                continue
            emitted_properties = {prop.property_name for prop in node.properties}
            configured_defaults = {
                attribute.technical_name
                for attribute, _ in self.registry.configured_defaults_for_class(
                    node.class_name
                )
            }
            checked: set[str] = set()
            for rule in ontology_class.rules:
                if rule.property in checked:
                    continue
                if rule.property in configured_defaults:
                    continue
                if not is_source_required_rule(self.registry, rule):
                    continue
                checked.add(rule.property)
                if rule.property in emitted_properties:
                    continue
                errors.append(
                    {
                        "code": "MISSING_REQUIRED_SOURCE_FACT",
                        "location": f"nodes.{node_index}.properties.{rule.property}",
                        "nodeTempId": node.temp_id,
                        "propertyName": rule.property,
                        "message": (
                            f"{node.class_name} node {node.temp_id} is newly introduced "
                            f"but lacks required source-backed property {rule.property}; "
                            "ground the property from the current source or do not create "
                            "this entity as an incomplete stub"
                        ),
                    }
                )
        return errors

    def _required_derived_edge_errors(
        self,
        fragment: GraphPatchFragment,
        *,
        existing_node_refs: set[str],
    ) -> list[dict[str, Any]]:
        """
        Báo lỗi khi fragment thiếu quan hệ cần thiết để suy diễn thuộc tính cho node.
        """
        errors: list[dict[str, Any]] = []
        incoming = {
            (edge.target_temp_id, edge.edge_name)
            for edge in fragment.edges
        }
        for node_index, node in enumerate(fragment.nodes):
            if node.temp_id in existing_node_refs:
                continue
            ontology_class = self.registry.get_class(node.class_name)
            if ontology_class is None:
                continue
            for rule in ontology_class.rules:
                is_required = rule.operator in {"some", "exactlyQualified"}
                if rule.operator == "minQualified":
                    try:
                        is_required = int(rule.value) > 0
                    except (TypeError, ValueError):
                        is_required = False
                if not is_required:
                    continue

                attribute = self.registry.get_attribute(rule.property)
                if attribute is None:
                    continue
                deriving_edges = self.registry.edge_names_deriving_property(
                    rule.property
                )
                if (
                    attribute.ingestion_policy.mode != "edge_derived"
                    and not deriving_edges
                ):
                    continue
                if any(
                    (node.temp_id, edge_name) in incoming
                    for edge_name in deriving_edges
                ):
                    continue

                errors.append(
                    {
                        "code": "DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE",
                        "location": (
                            f"nodes.{node_index}.properties.{rule.property}"
                        ),
                        "nodeTempId": node.temp_id,
                        "propertyName": rule.property,
                        "message": (
                            f"{node.class_name} node {node.temp_id} requires an "
                            f"incoming relationship deriving {rule.property}; "
                            f"allowed edges: {sorted(deriving_edges)}"
                        ),
                    }
                )
        return errors
