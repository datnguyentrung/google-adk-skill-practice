from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Iterable

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import Evidence, GraphPatchDraft
from app.core.schemas.ingestion.validation import ValidationIssue
from app.services.ingestion.registry import OntologyRegistry


class SourceGroundingValidator:
    """Validate chunk coverage and source evidence for an extraction draft."""

    def __init__(self, registry: OntologyRegistry):
        self.registry = registry

    def validate(
        self,
        draft: GraphPatchDraft,
        chunks: list[DocumentChunk],
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        chunk_by_index = {chunk.index: chunk for chunk in chunks}
        expected_indexes = set(chunk_by_index)
        coverage_by_index = {}
        for position, item in enumerate(draft.coverage):
            if item.chunk_index in coverage_by_index:
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_COVERAGE",
                        message=(
                            f"Chunk {item.chunk_index} appears more than once in coverage"
                        ),
                        location=f"coverage.{position}.chunkIndex",
                    )
                )
            coverage_by_index[item.chunk_index] = item

        supplied_indexes = set(coverage_by_index)
        for chunk_index in sorted(expected_indexes - supplied_indexes):
            issues.append(
                ValidationIssue(
                    code="COVERAGE_MISSING",
                    message=(
                        f"Prepared source chunk {chunk_index} is missing from coverage"
                    ),
                    location="coverage",
                )
            )
        for chunk_index in sorted(supplied_indexes - expected_indexes):
            issues.append(
                ValidationIssue(
                    code="COVERAGE_UNKNOWN_CHUNK",
                    message=f"Coverage references unknown source chunk {chunk_index}",
                    location=f"coverage.{chunk_index}",
                )
            )

        referenced_chunks: set[int] = set()
        for node_index, node in enumerate(draft.nodes):
            issues.extend(
                self._validate_evidence(
                    node.evidence,
                    chunk_by_index,
                    f"nodes.{node_index}.evidence",
                    referenced_chunks,
                    node_temp_id=node.temp_id,
                )
            )
            for property_index, entry in enumerate(node.properties):
                issues.extend(
                    self._validate_evidence(
                        entry.evidence,
                        chunk_by_index,
                        f"nodes.{node_index}.properties.{property_index}.evidence",
                        referenced_chunks,
                        node_temp_id=node.temp_id,
                        property_name=entry.property_name,
                    )
                )
                issues.extend(
                    self._validate_literal_sensitive_property(
                        entry.property_name,
                        entry.value,
                        entry.evidence,
                        chunk_by_index,
                        node_index,
                        property_index,
                        node.temp_id,
                    )
                )

        for edge_index, edge in enumerate(draft.edges):
            issues.extend(
                self._validate_evidence(
                    edge.evidence,
                    chunk_by_index,
                    f"edges.{edge_index}.evidence",
                    referenced_chunks,
                    edge_name=edge.edge_name,
                )
            )

        for chunk_index, item in coverage_by_index.items():
            if chunk_index not in expected_indexes:
                continue
            if item.decision == "MAPPED" and chunk_index not in referenced_chunks:
                issues.append(
                    ValidationIssue(
                        code="COVERAGE_NOT_EVIDENCED",
                        message=(
                            f"Chunk {chunk_index} is marked MAPPED but no node, "
                            "property, or edge evidence references it"
                        ),
                        location=f"coverage.{chunk_index}",
                    )
                )
            if item.decision == "NOT_RELEVANT" and chunk_index in referenced_chunks:
                issues.append(
                    ValidationIssue(
                        code="COVERAGE_CONFLICT",
                        message=(
                            f"Chunk {chunk_index} is marked NOT_RELEVANT but is "
                            "used as evidence"
                        ),
                        location=f"coverage.{chunk_index}",
                    )
                )

        return self._deduplicate(issues)

    def _validate_evidence(
        self,
        evidence_items: Iterable[Evidence],
        chunk_by_index: dict[int, DocumentChunk],
        location_prefix: str,
        referenced_chunks: set[int],
        *,
        node_temp_id: str | None = None,
        property_name: str | None = None,
        edge_name: str | None = None,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for index, evidence in enumerate(evidence_items):
            location = f"{location_prefix}.{index}"
            chunk = chunk_by_index.get(evidence.chunk_index)
            if chunk is None:
                issues.append(
                    ValidationIssue(
                        code="EVIDENCE_UNKNOWN_CHUNK",
                        message=(
                            f"Evidence references unknown chunk {evidence.chunk_index}"
                        ),
                        location=location,
                        node_temp_id=node_temp_id,
                        property_name=property_name,
                        edge_name=edge_name,
                    )
                )
                continue

            referenced_chunks.add(evidence.chunk_index)
            if evidence.source != chunk.source:
                issues.append(
                    ValidationIssue(
                        code="EVIDENCE_SOURCE_MISMATCH",
                        message=(
                            f"Evidence source {evidence.source!r} does not match "
                            f"prepared chunk source {chunk.source!r}"
                        ),
                        location=location,
                        node_temp_id=node_temp_id,
                        property_name=property_name,
                        edge_name=edge_name,
                    )
                )
            if evidence.section is not None and evidence.section != chunk.section:
                issues.append(
                    ValidationIssue(
                        code="EVIDENCE_SECTION_MISMATCH",
                        message=(
                            f"Evidence section {evidence.section!r} does not match "
                            f"chunk section {chunk.section!r}"
                        ),
                        location=location,
                        node_temp_id=node_temp_id,
                        property_name=property_name,
                        edge_name=edge_name,
                    )
                )
            if not self._contains_quote(chunk.content, evidence.text):
                issues.append(
                    ValidationIssue(
                        code="EVIDENCE_TEXT_NOT_IN_SOURCE",
                        message=(
                            "Evidence text is not a verbatim excerpt of chunk "
                            f"{evidence.chunk_index}"
                        ),
                        location=location,
                        node_temp_id=node_temp_id,
                        property_name=property_name,
                        edge_name=edge_name,
                    )
                )
        return issues

    def _validate_literal_sensitive_property(
        self,
        property_name: str,
        value,
        evidence_items: list[Evidence],
        chunk_by_index: dict[int, DocumentChunk],
        node_index: int,
        property_index: int,
        node_temp_id: str,
    ) -> list[ValidationIssue]:
        attribute = self.registry.get_attribute(property_name)
        if attribute is None:
            return []

        local_name = attribute.local_name.casefold().replace("_", "")
        literal_sensitive = (
            local_name.endswith("status")
            or local_name.endswith("code")
            or "versionnumber" in local_name
        )
        is_date = "xsd:date" in attribute.range
        if not (literal_sensitive or is_date):
            return []

        source_text = " ".join(
            chunk_by_index[item.chunk_index].content
            for item in evidence_items
            if item.chunk_index in chunk_by_index
        )
        if is_date:
            supported = self._date_supported(value, source_text)
        else:
            supported = self._normalize(str(value)) in self._normalize(source_text)
        if supported:
            return []

        return [
            ValidationIssue(
                code="PROPERTY_VALUE_NOT_GROUNDED",
                message=(
                    f"Literal-sensitive property {property_name}={value!r} is not "
                    "supported by its cited source chunk(s)"
                ),
                location=f"nodes.{node_index}.properties.{property_index}",
                node_temp_id=node_temp_id,
                property_name=property_name,
            )
        ]

    @classmethod
    def _contains_quote(cls, content: str, quote: str) -> bool:
        return cls._normalize(quote) in cls._normalize(content)

    @staticmethod
    def _normalize(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        return re.sub(r"\s+", " ", value).strip()

    @classmethod
    def _date_supported(cls, value, source_text: str) -> bool:
        if isinstance(value, date):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                return False
        else:
            return False

        candidates = {
            parsed.isoformat(),
            parsed.strftime("%d/%m/%Y"),
            parsed.strftime("%d-%m-%Y"),
            parsed.strftime("%d.%m.%Y"),
        }
        normalized_source = cls._normalize(source_text)
        return any(
            cls._normalize(candidate) in normalized_source
            for candidate in candidates
        )

    @staticmethod
    def _deduplicate(issues: list[ValidationIssue]) -> list[ValidationIssue]:
        unique: dict[tuple[str, str, str], ValidationIssue] = {}
        for issue in issues:
            unique[(issue.code, issue.location, issue.message)] = issue
        return list(unique.values())
