from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.schemas.ingestion.graph_patch import Evidence, GraphPatchDraft
from app.core.schemas.ingestion.validation import ValidationIssue


DEFAULT_ONTOLOGY_PATH = Path(
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)
COMPILER_SCHEMA_VERSION = "1"
NO_ARTIFACT_DIGEST = "NO_ARTIFACT"
RULE_TYPE_BY_EDGE = {
    "pskg:hasEligibilityRule": "ELIGIBILITY",
    "pskg:hasSalesConditionRule": "SALES_CONDITION",
    "pskg:governedByPolicy": "POLICY",
}


class _CompiledModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompiledNode(_CompiledModel):
    temp_id: str
    class_name: str
    properties: dict[str, Any]
    evidence: tuple[Evidence, ...]
    confidence: float


class CompiledEdge(_CompiledModel):
    edge_name: str
    source_temp_id: str
    target_temp_id: str
    evidence: tuple[Evidence, ...]
    confidence: float


class CompiledGraphPatch(_CompiledModel):
    nodes: tuple[CompiledNode, ...]
    edges: tuple[CompiledEdge, ...]
    warnings: tuple[str, ...] = Field(default_factory=tuple)


@dataclass(frozen=True)
class CompilerResult:
    compiled_patch: CompiledGraphPatch | None
    errors: tuple[ValidationIssue, ...]


@dataclass
class _NodeBuilder:
    temp_id: str
    class_name: str
    properties: dict[str, Any]
    evidence: tuple[Evidence, ...]
    confidence: float


class GraphPatchCompiler:
    def __init__(
        self,
        ontology_path: str | Path = DEFAULT_ONTOLOGY_PATH,
        schema_version: str = COMPILER_SCHEMA_VERSION,
    ):
        self.ontology_path = Path(ontology_path)
        self.schema_version = schema_version
        self.ontology_digest = hashlib.sha256(
            self.ontology_path.read_bytes()
        ).hexdigest()

    def compile(self, draft: GraphPatchDraft) -> CompilerResult:
        errors: list[ValidationIssue] = []
        node_builders: list[_NodeBuilder] = []
        seen_temp_ids: set[str] = set()

        for node_index, node in enumerate(draft.nodes):
            if node.temp_id in seen_temp_ids:
                errors.append(
                    ValidationIssue(
                        code="DUPLICATE_TEMP_ID",
                        message=f"Duplicate tempId: {node.temp_id}",
                        location=f"nodes.{node_index}.tempId",
                        node_temp_id=node.temp_id,
                    )
                )
            seen_temp_ids.add(node.temp_id)

            properties: dict[str, Any] = {}
            for property_index, entry in enumerate(node.properties):
                if entry.property_name not in properties:
                    properties[entry.property_name] = entry.value
                    continue

                if not self._values_identical(
                    properties[entry.property_name],
                    entry.value,
                ):
                    errors.append(
                        ValidationIssue(
                            code="DUPLICATE_PROPERTY",
                            message=(
                                f"Property {entry.property_name} has conflicting values "
                                f"on node {node.temp_id}"
                            ),
                            location=(
                                f"nodes.{node_index}.properties.{property_index}"
                            ),
                            node_temp_id=node.temp_id,
                            property_name=entry.property_name,
                        )
                    )

            node_builders.append(
                _NodeBuilder(
                    temp_id=node.temp_id,
                    class_name=node.class_name,
                    properties=properties,
                    evidence=tuple(node.evidence),
                    confidence=node.confidence,
                )
            )

        node_by_temp_id = {node.temp_id: node for node in node_builders}
        edges: list[CompiledEdge] = []
        for edge_index, edge in enumerate(draft.edges):
            source = node_by_temp_id.get(edge.source_temp_id)
            target = node_by_temp_id.get(edge.target_temp_id)
            if source is None:
                errors.append(
                    ValidationIssue(
                        code="DANGLING_REFERENCE",
                        message=f"Unknown sourceTempId: {edge.source_temp_id}",
                        location=f"edges.{edge_index}.sourceTempId",
                        edge_name=edge.edge_name,
                    )
                )
            if target is None:
                errors.append(
                    ValidationIssue(
                        code="DANGLING_REFERENCE",
                        message=f"Unknown targetTempId: {edge.target_temp_id}",
                        location=f"edges.{edge_index}.targetTempId",
                        edge_name=edge.edge_name,
                    )
                )

            expected_rule_type = RULE_TYPE_BY_EDGE.get(edge.edge_name)
            if target is not None and expected_rule_type is not None:
                actual_rule_type = target.properties.get("pskg:ruleType")
                if actual_rule_type is None:
                    target.properties["pskg:ruleType"] = expected_rule_type
                elif actual_rule_type != expected_rule_type:
                    errors.append(
                        ValidationIssue(
                            code="SEMANTIC_CONFLICT",
                            message=(
                                f"Edge {edge.edge_name} requires target "
                                f"pskg:ruleType={expected_rule_type}; got "
                                f"{actual_rule_type}"
                            ),
                            location=f"edges.{edge_index}",
                            node_temp_id=target.temp_id,
                            property_name="pskg:ruleType",
                            edge_name=edge.edge_name,
                        )
                    )

            edges.append(
                CompiledEdge(
                    edge_name=edge.edge_name,
                    source_temp_id=edge.source_temp_id,
                    target_temp_id=edge.target_temp_id,
                    evidence=tuple(edge.evidence),
                    confidence=edge.confidence,
                )
            )

        if errors:
            return CompilerResult(compiled_patch=None, errors=tuple(errors))

        nodes = tuple(
            CompiledNode(
                temp_id=node.temp_id,
                class_name=node.class_name,
                properties=dict(sorted(node.properties.items())),
                evidence=node.evidence,
                confidence=node.confidence,
            )
            for node in node_builders
        )
        return CompilerResult(
            compiled_patch=CompiledGraphPatch(
                nodes=nodes,
                edges=tuple(edges),
                warnings=tuple(draft.warnings),
            ),
            errors=(),
        )

    def fingerprint(
        self,
        patch: CompiledGraphPatch,
        artifact_content_digest: str | None,
    ) -> str:
        payload = {
            "artifactContentDigest": artifact_content_digest or NO_ARTIFACT_DIGEST,
            "compilerSchemaVersion": self.schema_version,
            "ontologyFileDigest": self.ontology_digest,
            "patch": self._canonical_patch(patch),
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def _values_identical(cls, left: Any, right: Any) -> bool:
        if type(left) is not type(right):
            return False
        if isinstance(left, dict):
            return left.keys() == right.keys() and all(
                cls._values_identical(left[key], right[key]) for key in left
            )
        if isinstance(left, list):
            return len(left) == len(right) and all(
                cls._values_identical(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        return left == right

    def _canonical_patch(self, patch: CompiledGraphPatch) -> dict[str, Any]:
        def canonical_evidence(items: tuple[Evidence, ...]) -> list[dict[str, Any]]:
            ordered = sorted(
                items,
                key=lambda item: (item.source, item.section or "", item.text),
            )
            return [
                {
                    "source": item.source,
                    "section": item.section,
                    "text": item.text,
                }
                for item in ordered
            ]

        nodes = [
            {
                "tempId": node.temp_id,
                "className": node.class_name,
                "properties": {
                    name: self._canonical_value(node.properties[name])
                    for name in sorted(node.properties)
                },
                "evidence": canonical_evidence(node.evidence),
                "confidence": node.confidence,
            }
            for node in sorted(patch.nodes, key=lambda item: item.temp_id)
        ]
        edges = [
            {
                "edgeName": edge.edge_name,
                "sourceTempId": edge.source_temp_id,
                "targetTempId": edge.target_temp_id,
                "evidence": canonical_evidence(edge.evidence),
                "confidence": edge.confidence,
            }
            for edge in sorted(
                patch.edges,
                key=lambda item: (
                    item.edge_name,
                    item.source_temp_id,
                    item.target_temp_id,
                ),
            )
        ]
        return {"nodes": nodes, "edges": edges, "warnings": list(patch.warnings)}

    @classmethod
    def _canonical_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._canonical_value(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [cls._canonical_value(item) for item in value]
        return value
