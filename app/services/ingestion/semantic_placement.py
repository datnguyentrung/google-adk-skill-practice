from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from pydantic import ValidationError

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import (
    Evidence,
    ExtractedNode,
    GraphPatchDraft,
    GraphPatchFragment,
)
from app.services.ingestion.graph_patch_compiler import GraphPatchCompiler
from app.services.ingestion.graph_validation import OntologyValidator, SourceGroundingValidator
from app.services.ingestion.model_call_control import AdkStructuredCallExecutor
from app.services.ingestion.registry import OntologyRegistry

logger = logging.getLogger(__name__)

DEFAULT_SEMANTIC_MAPPING_MODEL = os.getenv("INGESTION_MODEL", "gemini-3.1-flash-lite")


class DirectGraphMappingError(ValueError):
    retryable = True
    error_kind = "llm_mapping"

    def __init__(self, message: str, *, summary: dict[str, Any] | None = None):
        super().__init__(message)
        self.summary = summary or {}


class SemanticGraphMapper:
    """Map one source batch directly to an ontology-constrained graph fragment."""

    def __init__(
        self,
        *,
        registry: OntologyRegistry,
        compiler: GraphPatchCompiler,
        ontology_validator: OntologyValidator,
        model: str = DEFAULT_SEMANTIC_MAPPING_MODEL,
        structured_executor: AdkStructuredCallExecutor | None = None,
    ):
        self.registry = registry
        self.compiler = compiler
        self.ontology_validator = ontology_validator
        self.model = model
        self.structured_executor = structured_executor or AdkStructuredCallExecutor(
            model=self.model
        )
        self._ontology_projection = self._build_ontology_projection()

    def map_batch(
        self,
        *,
        batch_payload: dict[str, Any],
        chunks: list[DocumentChunk],
        graph_context: str | None = None,
        previous_error: dict[str, Any] | None = None,
    ) -> GraphPatchFragment:
        chunk_indexes = [chunk.index for chunk in chunks]
        prompt = self._prompt(
            batch_payload=batch_payload,
            chunks=chunks,
            graph_context=graph_context or "",
            previous_error=previous_error,
        )
        schema = self._response_schema(chunk_indexes)
        logger.info(
            "[PHASE:DIRECT_GRAPH_MAPPING_START] Func: map_batch | Batch: %s | Chunks: %s | OntologyChars: %s | PromptChars: %s",
            batch_payload.get("batchIndex"),
            chunk_indexes,
            len(self._ontology_projection),
            len(prompt),
        )
        try:
            payload = self.structured_executor.run(
                operation="direct_graph_mapping",
                instruction=prompt,
                output_schema=schema,
                message="Map this batch and return the GraphPatchFragment.",
            )
            fragment = GraphPatchFragment.model_validate(payload)
            fragment = self._resolve_context_nodes(
                fragment, graph_context or "", int(batch_payload.get("batchIndex", 0))
            )
            fragment = self._canonicalize_evidence(fragment, chunks)
            errors = self._validate_fragment(fragment, chunks)
            if errors:
                logger.error(
                    "[INGESTION_ERROR] Phase: DIRECT_GRAPH_MAPPING | Func: map_batch | Batch: %s | "
                    "Error: Fragment failed deterministic validation | Details: %s",
                    batch_payload.get("batchIndex"),
                    errors[:12],
                )
                raise DirectGraphMappingError(
                    "Direct graph fragment failed deterministic validation",
                    summary={"errors": errors[:12]},
                )
            logger.info(
                "[PHASE:DIRECT_GRAPH_MAPPING_SUCCESS] Func: map_batch | Batch: %s | Nodes: %s | Edges: %s | Coverage: %s",
                batch_payload.get("batchIndex"),
                len(fragment.nodes),
                len(fragment.edges),
                len(fragment.coverage),
            )
            return fragment
        except ValidationError as exc:
            logger.error(
                "[INGESTION_ERROR] Phase: DIRECT_GRAPH_MAPPING | Func: map_batch | Batch: %s | "
                "Error: Pydantic schema validation failed | Message: %s",
                batch_payload.get("batchIndex"),
                exc,
                exc_info=True,
            )
            raise DirectGraphMappingError(
                "Gemini returned an invalid GraphPatchFragment",
                summary={"validationErrors": exc.errors(include_url=False)[:8]},
            ) from exc
        except Exception as exc:
            if not isinstance(exc, DirectGraphMappingError):
                logger.error(
                    "[INGESTION_ERROR] Phase: DIRECT_GRAPH_MAPPING | Func: map_batch | Batch: %s | "
                    "Unexpected Error: %s",
                    batch_payload.get("batchIndex"),
                    exc,
                    exc_info=True,
                )
            raise

    def _prompt(
        self,
        *,
        batch_payload: dict[str, Any],
        chunks: list[DocumentChunk],
        graph_context: str,
        previous_error: dict[str, Any] | None,
    ) -> str:
        repair = (
            "\nPREVIOUS ATTEMPT FEEDBACK:\n"
            + json.dumps(previous_error, ensure_ascii=False)
            if previous_error
            else ""
        )
        return (
            "Map source chunks directly into the supplied ontology. Return exactly one "
            "GraphPatchFragment matching the response schema.\n\n"
            "ONTOLOGY COMPACT PROJECTION (authoritative; use only these technical names):\n"
            f"{self._ontology_projection}\n\n"
            "MAPPING RULES:\n"
            "- Use ontology definitions, domains, ranges and property meanings semantically; do not invent classes, properties or edges.\n"
            "- Represent every source-backed fact that has a direct ontology counterpart. Do not collapse a specific concept into a generic productAttributes value when a specific class/property/edge fits.\n"
            "- Do not create a node merely because a noun appears. Create reusable ontology entities only when the source meaning matches that class.\n"
            "- Treat a section heading as source evidence for the concept it names when the section body describes that concept; the heading may supply the entity name while the body supplies its description or relationships.\n"
            "- Process every supplied chunk exhaustively. Parallel/repeated sections that each define a distinct reusable ontology concept must produce distinct graph entities; do not stop after mapping the first example.\n"
            "- If an entire chunk is itself a structured reusable content asset (for example a dialogue, Q&A, procedure, guide, script, or knowledge item) and an ontology class definition covers that kind of asset, represent the whole chunk as that class before considering embedded facts. Do not reduce the container to only one fact mentioned inside it.\n"
            "- Use NOT_RELEVANT only after considering both the section heading and body and finding no direct class, property, or edge counterpart in the ontology.\n"
            "- For a new target entity, include its source-backed identity/content properties when the ontology exposes them (for example *Name/*Title/content). Avoid empty placeholder nodes when the source gives identifying content.\n"
            "- For properties marked grounding=source_literal, the property VALUE itself must be directly supported by the cited chunk text or section heading. For xsd:string source_literal properties, the VALUE must appear verbatim inside one cited evidence.text; never summarize or combine source facts into a synthetic string. Do not invent or infer labels, types, titles, scenarios, priorities, names, or other literal values. Omit an optional source_literal property rather than synthesize it. If a new class requires a source_literal property and the current source/context does not state it, do not create that node in this batch.\n"
            "- REQUIRED entries are ontology persistence constraints. Never fabricate a value or relationship merely to satisfy one. A canonical context entity may already satisfy a requirement; for a new node, if a required source attribute is absent from the current source/context, choose another valid representation or omit that node.\n"
            "- Do not emit runtime/default/derived properties. The compiler derives those from ontology policy and edges.\n"
            "- Edge direction MUST follow ontology domain -> range, regardless of sentence word order.\n"
            "- tempId is fragment-local unless you copy an exact ref from CANONICAL GRAPH CONTEXT. Reuse a canonical ref only for the same entity; a related rule/topic with different meaning is a new entity and needs a new tempId.\n"
            "- Every node/property/edge must carry evidence.text copied verbatim from one supplied chunk. Evidence text must be an exact substring of that chunk, not a paraphrase.\n"
            "- coverage must contain exactly one entry for every supplied chunk index. Coverage is graph accounting, not a statement that you read the chunk. Set MAPPED if and only if at least one emitted node evidence, property evidence, or edge evidence has that exact chunkIndex. If no emitted graph element cites a chunk, MAPPED is forbidden. Use NOT_RELEVANT when the chunk has no direct ontology-mappable fact, UNSUPPORTED_BY_ONTOLOGY when it has a business fact the ontology cannot represent, or DUPLICATE_EVIDENCE when it only repeats an already represented fact.\n"
            "- When the source states a rule/threshold/obligation, choose the ontology rule edge whose definition matches its semantic type; do not create a BusinessRule for ordinary descriptive capabilities. Do not infer a specialized relationship subtype from a generic reference. If the source merely identifies a policy/reference and does not state eligibility or sales-condition semantics, choose the ontology relationship whose definition represents governance/reference rather than a more specific subtype.\n"
            "- For properties marked grounding=source_literal, the property value must be directly supported by its evidence; never invent a label, priority, scenario, title, version, or content summary. Dates/numbers may be normalized only to the ontology datatype.\n"
            "- Before returning, audit coverage against your own graph output: every MAPPED chunkIndex must appear in at least one emitted node/property/edge evidence item; every chunk that contributes no graph evidence must use a non-MAPPED decision.\n- Preserve dates and numeric values in ontology-compatible JSON datatypes/formats.\n\n"
            "CANONICAL GRAPH CONTEXT FROM EARLIER BATCHES:\n"
            f"{graph_context or '(none)'}"
            f"{repair}\n\n"
            "SOURCE BATCH:\n"
            f"{json.dumps(batch_payload, ensure_ascii=False)}"
        )

    def _build_ontology_projection(self) -> str:
        lines = ["CLASSES"]
        for name in self.registry.list_classes():
            cls = self.registry.get_class(name)
            if cls is None:
                continue
            lines.append(
                f"- {cls.technical_name} | {cls.label} | {_short_definition(cls.definition)}"
            )
        lines.append("EDGES")
        for name in self.registry.list_edges():
            edge = self.registry.get_edge(name)
            if edge is None:
                continue
            derived = self.registry.derived_target_properties_for_edge(name)
            derived_text = ""
            if derived:
                derived_text = " | derives=" + ", ".join(
                    f"{attribute.technical_name}={value}"
                    for attribute, value in derived
                )
            lines.append(
                f"- {edge.technical_name} | {','.join(edge.domain)} -> {','.join(edge.range)} "
                f"| {edge.label} | {_short_definition(edge.definition)}{derived_text}"
            )
        lines.append("SOURCE PROPERTIES")
        for class_name in self.registry.list_classes():
            cls = self.registry.get_class(class_name)
            if cls is None:
                continue
            required = []
            for rule in cls.rules:
                is_required = rule.operator in {"some", "exactlyQualified"}
                if rule.operator == "minQualified":
                    try:
                        is_required = int(rule.value) > 0
                    except (TypeError, ValueError):
                        is_required = False
                if is_required:
                    qualifier = (
                        f" | qualifier={rule.qualifier}" if rule.qualifier else ""
                    )
                    required.append(
                        f"  REQUIRED {rule.property} | {rule.operator}={rule.value}{qualifier}"
                    )
            properties = []
            for attr in self.registry.properties_from_class(class_name):
                if (
                    attr.ingestion_policy.mode != "source"
                    or self.registry.is_runtime_managed_attribute(attr.technical_name)
                    or self.registry.edge_names_deriving_property(attr.technical_name)
                ):
                    continue
                properties.append(
                    "  - "
                    f"{attr.technical_name} | range={','.join(attr.range)} "
                    f"| grounding={attr.ingestion_policy.grounding} "
                    f"| {attr.label} | {_short_definition(attr.definition)}"
                )
            if required or properties:
                lines.append(f"CLASS {cls.technical_name}")
                lines.extend(required)
                lines.extend(properties)
        return "\n".join(lines)

    def _response_schema(self, chunk_indexes: list[int]) -> dict[str, Any]:
        schema = GraphPatchFragment.model_json_schema(by_alias=True)
        defs = schema["$defs"]
        class_field = defs["ExtractedNode"]["properties"]["className"]
        class_field.pop("pattern", None)
        class_field["enum"] = self.registry.list_classes()
        property_field = defs["ExtractedProperty"]["properties"]["propertyName"]
        property_field.pop("pattern", None)
        property_field["enum"] = self.registry.list_attributes()
        edge_field = defs["ExtractedEdge"]["properties"]["edgeName"]
        edge_field.pop("pattern", None)
        edge_field["enum"] = self.registry.list_edges()
        defs["ChunkCoverage"]["properties"]["chunkIndex"] = {
            "type": "integer",
            "enum": chunk_indexes,
        }
        defs["Evidence"]["properties"]["chunkIndex"] = {
            "type": "integer",
            "enum": chunk_indexes,
        }
        defs["ExtractedProperty"]["properties"]["value"] = {
            "anyOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "integer"},
                {"type": "boolean"},
                {
                    "type": "array",
                    "items": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "number"},
                            {"type": "integer"},
                            {"type": "boolean"},
                        ]
                    },
                },
            ]
        }
        return schema

    def _resolve_context_nodes(
        self,
        fragment: GraphPatchFragment,
        graph_context: str,
        batch_index: int,
    ) -> GraphPatchFragment:
        known: dict[str, tuple[str, dict[str, Any]]] = {}
        for match in re.finditer(
            r"- ref=(\S+)\n\s+class=(\S+)\n\s+identity=(\{.*\})",
            graph_context,
        ):
            try:
                identity = json.loads(match.group(3))
            except json.JSONDecodeError:
                identity = {}
            known[match.group(1)] = (match.group(2), identity)

        renamed: dict[str, str] = {}
        nodes: list[ExtractedNode] = []
        for node in fragment.nodes:
            context_node = known.get(node.temp_id)
            keep_context_ref = False
            if context_node is not None:
                context_class, context_identity = context_node
                if context_class != node.class_name:
                    keep_context_ref = True
                else:
                    incoming = {
                        prop.property_name: prop.value for prop in node.properties
                    }
                    overlaps = set(incoming) & set(context_identity)
                    matches = {
                        name
                        for name in overlaps
                        if _stable_value(incoming[name])
                        == _stable_value(context_identity[name])
                    }
                    conflicts = overlaps - matches
                    keep_context_ref = not conflicts or bool(matches)

            new_id = (
                node.temp_id if keep_context_ref else f"b{batch_index}__{node.temp_id}"
            )
            if new_id != node.temp_id:
                renamed[node.temp_id] = new_id
                node = node.model_copy(update={"temp_id": new_id})
            nodes.append(node)

        edges = []
        for edge in fragment.edges:
            edges.append(
                edge.model_copy(
                    update={
                        "source_temp_id": renamed.get(
                            edge.source_temp_id, edge.source_temp_id
                        ),
                        "target_temp_id": renamed.get(
                            edge.target_temp_id, edge.target_temp_id
                        ),
                    }
                )
            )

        existing = {node.temp_id for node in nodes}
        for edge in edges:
            for temp_id in (edge.source_temp_id, edge.target_temp_id):
                context_node = known.get(temp_id)
                if temp_id in existing or context_node is None:
                    continue
                class_name, _ = context_node
                nodes.append(
                    ExtractedNode(
                        tempId=temp_id,
                        className=class_name,
                        properties=[],
                        evidence=[item.model_copy(deep=True) for item in edge.evidence],
                        confidence=edge.confidence,
                    )
                )
                existing.add(temp_id)
        return fragment.model_copy(update={"nodes": nodes, "edges": edges})

    def _canonicalize_evidence(
        self, fragment: GraphPatchFragment, chunks: list[DocumentChunk]
    ) -> GraphPatchFragment:
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
        cited_chunks = {
            evidence.chunk_index
            for node in nodes
            for evidence in node.evidence
        } | {
            evidence.chunk_index
            for node in nodes
            for prop in node.properties
            for evidence in prop.evidence
        } | {
            evidence.chunk_index
            for edge in edges
            for evidence in edge.evidence
        }
        coverage = []
        for item in fragment.coverage:
            if item.chunk_index in cited_chunks and item.decision != "MAPPED":
                item = item.model_copy(update={"decision": "MAPPED", "reason": "Graph evidence emitted for this chunk"})
            elif item.chunk_index not in cited_chunks and item.decision == "MAPPED":
                item = item.model_copy(update={"decision": "AMBIGUOUS", "reason": "Mapper marked MAPPED but emitted no graph evidence for this chunk"})
            coverage.append(item)
        return fragment.model_copy(update={"nodes": nodes, "edges": edges, "coverage": coverage})

    def _validate_fragment(
        self, fragment: GraphPatchFragment, chunks: list[DocumentChunk]
    ) -> list[dict[str, Any]]:
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

        fact_chunks = {
            evidence.chunk_index
            for node in fragment.nodes
            for evidence in node.evidence
        } | {
            evidence.chunk_index
            for node in fragment.nodes
            for prop in node.properties
            for evidence in prop.evidence
        } | {
            evidence.chunk_index
            for edge in fragment.edges
            for evidence in edge.evidence
        }
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


def _canonical_whitespace_excerpt(chunk: DocumentChunk, quote: str) -> str:
    if not quote.strip():
        return quote
    parts = [re.escape(part) for part in re.split(r"\s+", quote.strip()) if part]
    if not parts:
        return quote
    pattern = r"\s+".join(parts)
    for surface in (chunk.section or "", chunk.content):
        match = re.search(pattern, surface, flags=re.MULTILINE)
        if match is not None:
            return match.group(0)
    return quote


def _canonical_table_excerpt(chunk: DocumentChunk, quote: str) -> str:
    normalized = quote.replace("\r\n", "\n").replace("\r", "\n").strip()
    if "|" not in normalized:
        return quote
    for line in chunk.content.splitlines():
        row = line.strip()
        if row.startswith("|") and row.endswith("|") and normalized in row:
            return row
    return quote


def _all_evidence(fragment: GraphPatchFragment):
    for node_index, node in enumerate(fragment.nodes):
        for item in node.evidence:
            yield f"nodes.{node_index}.evidence", item
        for property_index, prop in enumerate(node.properties):
            for item in prop.evidence:
                yield f"nodes.{node_index}.properties.{property_index}.evidence", item
    for edge_index, edge in enumerate(fragment.edges):
        for item in edge.evidence:
            yield f"edges.{edge_index}.evidence", item


def _short_definition(value: str) -> str:
    text = str(value or "").split("[Business constraint]", 1)[0].strip()
    return re.sub(r"\s+", " ", text)


def _stable_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


__all__ = ["DirectGraphMappingError", "SemanticGraphMapper"]
