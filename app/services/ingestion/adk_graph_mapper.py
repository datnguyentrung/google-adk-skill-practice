from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from pydantic import ValidationError

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import ExtractedNode, GraphPatchFragment
from app.services.ingestion.graph_fragment_guard import GraphFragmentGuard
from app.services.ingestion.graph_patch_compiler import GraphPatchCompiler
from app.services.ingestion.graph_validation import OntologyValidator
from app.services.ingestion.model_call_control import AdkStructuredCallExecutor
from app.services.ingestion.registry import OntologyRegistry

logger = logging.getLogger(__name__)
DEFAULT_GRAPH_MAPPING_MODEL = os.getenv(
    "INGESTION_MODEL", os.getenv("GOOGLE_ADK_MODEL", "gemini-3.5-flash-lite")
)


class DirectGraphMappingError(ValueError):
    retryable = True
    error_kind = "llm_mapping"

    def __init__(self, message: str, *, summary: dict[str, Any] | None = None):
        super().__init__(message)
        self.summary = summary or {}


class AdkGraphMapper:
    """Direct ADK mapper: source batch + ontology -> GraphPatchFragment."""

    def __init__(
        self,
        *,
        registry: OntologyRegistry,
        compiler: GraphPatchCompiler,
        ontology_validator: OntologyValidator,
        model: str = DEFAULT_GRAPH_MAPPING_MODEL,
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
        self.guard = GraphFragmentGuard(
            registry=self.registry,
            compiler=self.compiler,
            ontology_validator=self.ontology_validator,
        )

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
            fragment = self.guard.canonicalize(fragment, chunks)
            existing_node_refs = set(
                re.findall(r"(?m)^- ref=(\S+)$", graph_context or "")
            )
            errors = self.guard.validate(
                fragment,
                chunks,
                existing_node_refs=existing_node_refs,
            )
            if errors:
                logger.error(
                    "[INGESTION_ERROR] Phase: DIRECT_GRAPH_MAPPING | Func: map_batch | Batch: %s | "
                    "Error: Fragment failed deterministic validation | Details: %s",
                    batch_payload.get("batchIndex"),
                    errors[:12],
                )
                raise DirectGraphMappingError(
                    "Direct graph fragment failed deterministic validation",
                    summary={
                        "errors": errors[:12],
                        "candidateFragment": fragment.model_dump(
                            by_alias=True, mode="json", exclude_none=True
                        ),
                    },
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
        is_final_coverage_repair = bool(
            previous_error
            and previous_error.get("stage") == "final_coverage_validation"
        )
        repair_targets: list[int] = []
        feedback = previous_error
        if is_final_coverage_repair and previous_error is not None:
            for error in previous_error.get("errors", []):
                if not isinstance(error, dict):
                    continue
                match = re.fullmatch(
                    r"coverage\.(\d+)", str(error.get("location") or "")
                )
                if match is not None:
                    repair_targets.append(int(match.group(1)))
            repair_targets = sorted(set(repair_targets))
            feedback = json.loads(json.dumps(previous_error, ensure_ascii=False))
            candidate = feedback.get("candidateFragment")
            if isinstance(candidate, dict):
                targets = set(repair_targets)
                candidate["coverage"] = [
                    item
                    for item in candidate.get("coverage", [])
                    if not isinstance(item, dict)
                    or item.get("chunkIndex") not in targets
                ]

        repair = (
            "\nPREVIOUS ATTEMPT FEEDBACK / BASELINE:\n"
            + json.dumps(feedback, ensure_ascii=False)
            if feedback
            else ""
        )
        target_by_index = {chunk.index: chunk for chunk in chunks}
        target_blocks = []
        for chunk_index in repair_targets:
            chunk = target_by_index.get(chunk_index)
            if chunk is None:
                continue
            target_blocks.append(
                f"TARGET CHUNK {chunk.index}\n"
                f"SECTION: {chunk.section or '(none)'}\n"
                f"SOURCE: {chunk.source}\n"
                f"CONTENT:\n{chunk.content}"
            )
        final_coverage_repair = (
            "\nFINAL COVERAGE REPAIR MODE (authoritative):\n"
            f"- Repair targets: {repair_targets}. The prior target coverage decisions/reasons "
            "were removed from the baseline because final validation rejected them.\n"
            "- Re-evaluate EACH target directly from its verbatim source below against every "
            "available ontology class, property, and edge. Do not infer irrelevance from the "
            "old candidate fragment.\n"
            "- A successful repair for a target is MAPPED with grounded graph evidence, or "
            "DUPLICATE_EVIDENCE only when the same fact is already represented in canonical "
            "graph context. NOT_RELEVANT, NO_RELEVANT_FACT, UNSUPPORTED_BY_ONTOLOGY, "
            "AMBIGUOUS, or FAILED remain unresolved and will be rejected.\n"
            "- Before choosing a class for a repair target, inspect every REQUIRED_SOURCE "
            "entry shown for that class. If the current source/canonical context cannot ground "
            "a REQUIRED_SOURCE value, do not fabricate it and do not keep retrying that class; "
            "choose another ontology representation whose required source fields are actually "
            "groundable.\n"
            "- BusinessRule explicitly includes policy logic in addition to eligibility and "
            "sales conditions. Policy logic does NOT need eligibility semantics. Descriptive "
            "or operational wording may still define a governed product policy/condition when "
            "the ontology definition and relationship fit.\n"
            "- When a target names documents that may/must be provided, compare each named "
            "item with RequiredDocument and use unambiguous document/canonical entity scope "
            "for the relationship; do not require the product name to be repeated in the chunk.\n"
            "- Prefer the most specific ontology representation over encoding a specific entity "
            "or enumerated list only as a generic BusinessRule. If RequiredDocument plus its "
            "product relationship fits an enumerated document list, emit one RequiredDocument "
            "per groundable document item; use BusinessRule only for additional conditional logic.\n"
            "- Customer-facing product operating terms such as statement/billing cycles, default "
            "dates, statement contents, and credit-limit review rules are not irrelevant merely "
            "because they are explanatory or operational. Compare them to BusinessRule policy/"
            "sales-condition semantics and SalesKnowledge before rejecting them.\n"
            "- Never fabricate a generic SalesKnowledge knowledgeType such as 'Guide' when the "
            "source does not literally provide it. If SalesKnowledge REQUIRED_SOURCE fields cannot "
            "be grounded, choose another compatible ontology representation.\n"
            "- MAPPED is necessary but not sufficient: exhaustively represent every distinct "
            "source-backed fact inside each target that has a direct ontology counterpart. Do "
            "not stop after finding the first mappable fact in a target chunk.\n"
            "- When a target contains a list of independently meaningful named items and an "
            "ontology class applies per item, emit every source-backed item that can be grounded; "
            "do not map only the first list item.\n"
            "- Coverage decisions for NON-TARGET chunks must remain exactly equal to the baseline "
            "coverage decisions. Do not relabel or discard unrelated already-valid facts.\n"
            "- Preserve all unrelated valid baseline nodes, edges, evidence, identities, and "
            "coverage entries; repair only the target semantics and any facts necessary to "
            "support them.\n\n" + "\n\n".join(target_blocks) + "\n"
            if is_final_coverage_repair
            else ""
        )
        return (
            "Map source chunks directly into the supplied ontology. Return exactly one "
            "GraphPatchFragment matching the response schema.\n\n"
            "ONTOLOGY COMPACT PROJECTION (authoritative; use only these technical names):\n"
            f"{self._ontology_projection}\n\n"
            f"{final_coverage_repair}"
            "MAPPING RULES:\n"
            "- Use ontology definitions, domains, ranges and property meanings semantically; do not invent classes, properties or edges.\n"
            "- Represent every source-backed fact that has a direct ontology counterpart. Do not collapse a specific concept into a generic productAttributes value when a specific class/property/edge fits.\n"
            "- Do not create a node merely because a noun appears. Create reusable ontology entities only when the source meaning matches that class.\n"
            "- Treat a section heading as source evidence for the concept it names when the section body describes that concept; the heading may supply the entity name while the body supplies its description or relationships.\n"
            "- Process every supplied chunk exhaustively. Parallel/repeated sections that each define a distinct reusable ontology concept must produce distinct graph entities; do not stop after mapping the first example.\n"
            "- If an entire chunk is itself a structured reusable content asset (for example a dialogue, Q&A, procedure, guide, script, or knowledge item) and an ontology class definition covers that kind of asset, represent the whole chunk as that class before considering embedded facts. Do not reduce the container to only one fact mentioned inside it.\n"
            "- Use NOT_RELEVANT only after considering both the section heading and body and finding no direct class, property, or edge counterpart in the ontology.\n"
            "- For a new target entity, include its source-backed identity/content properties when the ontology exposes them (for example *Name/*Title/content). Avoid empty placeholder nodes when the source gives identifying content.\n"
            "- grounding=source_literal is a strict copy contract, not a summarization field. For xsd:string, VALUE must be one contiguous verbatim substring of one cited evidence.text. Never join rows/bullets, paraphrase, label, summarize, or construct a new string. Generic/fallback source_literal properties are NOT containers for synthesized summaries. If no exact literal exists, omit the optional property. If a new node requires a missing source_literal field, do not create that node.\n"
            "- REQUIRED_SOURCE entries must be grounded in the current source or canonical context; never fabricate them. If a genuinely source-required value is absent, choose another valid representation or omit that node.\n"
            "- REQUIRED_DERIVED entries are satisfied through ontology edges that derive the property. REQUIRED_SYSTEM entries are compiler/runtime/default-owned. Do not emit either kind and do not treat their absence from source text as a blocker. REQUIRED_EDGE entries require the corresponding grounded relationship.\n"
            "- Do not emit runtime/default/derived properties. The compiler derives those from ontology policy and edges.\n"
            "- Edge direction MUST follow ontology domain -> range, regardless of sentence word order.\n"
            "- tempId is fragment-local unless you copy an exact ref from CANONICAL GRAPH CONTEXT. Reuse a canonical ref only for the same entity; a related rule/topic with different meaning is a new entity and needs a new tempId.\n- When reusing a canonical ref, its existing scalar properties are already populated. Do not re-emit a scalar property with a different abbreviated or alternate value. Emit only exact same values or genuinely new non-conflicting properties; if the source proves a distinct entity, create a new node with its own identity.\n"
            "- Every node/property/edge must carry evidence.text copied verbatim from one supplied chunk. Evidence text must be an exact substring of that chunk, not a paraphrase.\n"
            "- coverage must contain exactly one entry for every supplied chunk index. Coverage is graph accounting, not a statement that you read the chunk. Set MAPPED if and only if at least one emitted node evidence, property evidence, or edge evidence has that exact chunkIndex. If no emitted graph element cites a chunk, MAPPED is forbidden. Use NOT_RELEVANT when the chunk has no direct ontology-mappable fact, UNSUPPORTED_BY_ONTOLOGY when it has a business fact the ontology cannot represent, or DUPLICATE_EVIDENCE when it only repeats an already represented fact.\n"
            "- When the source states a rule/threshold/obligation, choose the ontology rule edge whose definition matches its semantic type; do not create a BusinessRule for ordinary descriptive capabilities. Do not infer a specialized relationship subtype from a generic reference. If the source merely identifies a policy/reference and does not state eligibility or sales-condition semantics, choose the ontology relationship whose definition represents governance/reference rather than a more specific subtype.\n"
            "- Structured conditional content is not merely descriptive. For explicit eligibility criteria, obligations, consequences, thresholds, installment conditions, fee schedules, or conditional tables, inspect BusinessRule and its ontology edges before using UNSUPPORTED_BY_ONTOLOGY. Represent each independently actionable rule/row when appropriate, or one rule for a logically complete grouped condition, using source-normalized businessRuleCondition plus exact evidence.\n"
            "- UNSUPPORTED_BY_ONTOLOGY means no available ontology class/property/edge can represent the source fact. The absence of a dedicated scalar attribute is NOT enough to declare unsupported when BusinessRule.businessRuleCondition plus an appropriate rule edge can represent the condition, eligibility logic, consequence, threshold, or conditional schedule.\n"
            "- Bank discretion, review criteria, adjustment criteria, and conditional product policies are policy logic when the BusinessRule definition fits. Do not call them unsupported merely because there is no dedicated scalar product field; use the ontology policy relationship when its definition matches.\n"
            "- Descriptive or operational wording is not itself a reason to reject a fact. When source text defines how a product is determined, reviewed, adjusted, calculated, scheduled, billed, permitted, required, or governed, compare it to BusinessRule policy/sales-condition semantics and the available ontology edges before declaring it unsupported.\n"
            "- Use unambiguous document and section scope together with CANONICAL GRAPH CONTEXT. In a source artifact scoped to one canonical product/entity, a fact does not need to repeat that entity name in every chunk before it can be linked to it. Do not make this attachment when scope is ambiguous.\n"
            "- When the source explicitly names documents a customer may or must provide, inspect RequiredDocument and its product/document relationships before using UNSUPPORTED_BY_ONTOLOGY. A conditional document requirement is still representable when the ontology definitions and source-literal identity fields can be grounded.\n"
            "- For a list of independently named required/supporting documents, prefer one RequiredDocument entity per source-literal document item when each item is independently meaningful. Never concatenate several source-literal document names into one synthesized identity.\n"
            "- Prefer a specific ontology class/relationship over a generic BusinessRule when both can encode the same source fact. For an enumerated document list, emit one RequiredDocument per independently groundable document item when requiresDocument fits; use BusinessRule only for additional conditional logic.\n"
            "- Product operating terms such as statement or billing cycles, default dates, statement contents, and credit-limit review criteria must be compared to BusinessRule policy/sales-condition semantics and SalesKnowledge before being labeled NOT_RELEVANT or UNSUPPORTED.\n"
            "- Do not fabricate SalesKnowledge REQUIRED_SOURCE fields such as a generic knowledgeType. If those source literals are absent, choose another ontology representation whose required source fields are groundable.\n"
            "- A product FAQ, Q&A, explanatory section, guide section, or reusable answer asset may be SalesKnowledge when that class definition fits, even if facts inside it overlap rules already represented elsewhere. Preserve the source-backed knowledge container rather than rejecting it as merely descriptive.\n"
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
            configured_defaults = {
                attribute.technical_name
                for attribute, _ in self.registry.configured_defaults_for_class(
                    class_name
                )
            }
            for rule in cls.rules:
                is_required = rule.operator in {"some", "exactlyQualified"}
                if rule.operator == "minQualified":
                    try:
                        is_required = int(rule.value) > 0
                    except (TypeError, ValueError):
                        is_required = False
                if not is_required:
                    continue
                qualifier = f" | qualifier={rule.qualifier}" if rule.qualifier else ""
                attribute = self.registry.get_attribute(rule.property)
                if attribute is not None:
                    if (
                        self.registry.is_runtime_managed_attribute(rule.property)
                        or rule.property in configured_defaults
                        or attribute.ingestion_policy.mode
                        in {"runtime_managed", "system_default"}
                    ):
                        requirement_kind = "SYSTEM"
                    elif (
                        attribute.ingestion_policy.mode == "edge_derived"
                        or self.registry.edge_names_deriving_property(rule.property)
                    ):
                        requirement_kind = "DERIVED"
                    else:
                        requirement_kind = "SOURCE"
                elif self.registry.get_edge(rule.property) is not None:
                    requirement_kind = "EDGE"
                else:
                    requirement_kind = "OTHER"
                required.append(
                    f"  REQUIRED_{requirement_kind} {rule.property} | "
                    f"{rule.operator}={rule.value}{qualifier}"
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
                if context_class == node.class_name:
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
                node.temp_id
                if keep_context_ref
                else _batch_scoped_temp_id(node.temp_id, batch_index)
            )
            base_id = _strip_repeated_batch_prefix(node.temp_id, batch_index)
            collapsed_id = _batch_scoped_temp_id(node.temp_id, batch_index)
            renamed[node.temp_id] = new_id
            renamed[base_id] = new_id
            renamed[collapsed_id] = new_id
            if new_id != node.temp_id:
                node = node.model_copy(update={"temp_id": new_id})
            nodes.append(node)

        def resolve_endpoint(temp_id: str) -> str:
            resolved = renamed.get(temp_id, temp_id)
            if resolved in known:
                return resolved
            return _batch_scoped_temp_id(resolved, batch_index)

        edges = []
        for edge in fragment.edges:
            edges.append(
                edge.model_copy(
                    update={
                        "source_temp_id": resolve_endpoint(edge.source_temp_id),
                        "target_temp_id": resolve_endpoint(edge.target_temp_id),
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


def _batch_prefix(batch_index: int) -> str:
    return f"b{batch_index}__"


def _strip_repeated_batch_prefix(temp_id: str, batch_index: int) -> str:
    prefix = _batch_prefix(batch_index)
    while temp_id.startswith(prefix):
        temp_id = temp_id[len(prefix) :]
    return temp_id


def _batch_scoped_temp_id(temp_id: str, batch_index: int) -> str:
    return (
        f"{_batch_prefix(batch_index)}"
        f"{_strip_repeated_batch_prefix(temp_id, batch_index)}"
    )


def _short_definition(value: str) -> str:
    text = str(value or "").split("[Business constraint]", 1)[0].strip()
    return re.sub(r"\s+", " ", text)


def _stable_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


__all__ = ["AdkGraphMapper", "DirectGraphMappingError"]
