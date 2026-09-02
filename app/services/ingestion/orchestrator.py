from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Protocol

from google import genai
from google.genai import types
from pydantic import ValidationError

from app.core.schemas.ingestion.graph_patch import GraphPatchFragment

logger = logging.getLogger(__name__)

_SENSITIVE_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*\S+|api[_-]?key\s*[:=]\s*\S+|"
    r"token\s*[:=]\s*\S+|password\s*[:=]\s*\S+|pin\s*[:=]\s*\S+|"
    r"otp\s*[:=]\s*\S+)"
)

DEFAULT_INGESTION_MODEL = os.getenv(
    "INGESTION_ORCHESTRATOR_MODEL",
    os.getenv("GOOGLE_ADK_MODEL", "gemini-3.1-flash-lite"),
)
DEFAULT_INGESTION_TIMEOUT_SECONDS = float(
    os.getenv("INGESTION_ORCHESTRATOR_TIMEOUT_SECONDS", "120")
)


def _safe_preview(value: Any, *, limit: int = 500) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = _SENSITIVE_PATTERN.sub("<REDACTED>", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return f"{text[:half]} ... {text[-half:]}"


def _payload_stats(payload: Any) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(by_alias=True, mode="json")
    if not isinstance(payload, dict):
        return {"kind": type(payload).__name__}
    nodes = payload.get("nodes", []) or []
    edges = payload.get("edges", []) or []
    coverage = payload.get("coverage", []) or []
    return {
        "nodes": len(nodes) if isinstance(nodes, list) else None,
        "edges": len(edges) if isinstance(edges, list) else None,
        "properties": sum(
            len(node.get("properties", []) or [])
            for node in nodes
            if isinstance(node, dict)
        )
        if isinstance(nodes, list)
        else None,
        "coverage": len(coverage) if isinstance(coverage, list) else None,
    }


def _log_raw_payload(tag: str, payload: Any, *, batch: Any = None) -> None:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(by_alias=True, mode="json")
    logger.debug("[%s] batch=%s stats=%s", tag, batch, _payload_stats(payload))
    if not isinstance(payload, dict):
        logger.debug("[%s] batch=%s payload=%r", tag, batch, _safe_preview(payload))
        return
    for node in payload.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        logger.debug(
            "[EXTRACTION_NODE] batch=%s node_id=%s class=%s property_count=%s",
            batch,
            node.get("tempId") or node.get("temp_id"),
            node.get("className") or node.get("class_name"),
            len(node.get("properties", []) or []),
        )
        for prop in node.get("properties", []) or []:
            if not isinstance(prop, dict):
                continue
            evidence = prop.get("evidence", []) or []
            logger.debug(
                "[EXTRACTION_PROPERTY] batch=%s node_id=%s property=%s value=%r "
                "evidence_count=%s evidence=%s",
                batch,
                node.get("tempId") or node.get("temp_id"),
                prop.get("propertyName") or prop.get("property_name"),
                _safe_preview(prop.get("value")),
                len(evidence) if isinstance(evidence, list) else None,
                [
                    {
                        "chunkIndex": item.get("chunkIndex")
                        or item.get("chunk_index"),
                        "section": item.get("section"),
                        "text": _safe_preview(item.get("text") or item.get("content")),
                    }
                    for item in evidence
                    if isinstance(item, dict)
                ]
                if isinstance(evidence, list)
                else None,
            )
    for edge in payload.get("edges", []) or []:
        if not isinstance(edge, dict):
            continue
        logger.debug(
            "[EXTRACTION_EDGE] batch=%s edge=%s source_node=%s target_node=%s evidence=%s",
            batch,
            edge.get("edgeName") or edge.get("edge_name"),
            edge.get("sourceTempId") or edge.get("source_temp_id"),
            edge.get("targetTempId") or edge.get("target_temp_id"),
            [
                {
                    "chunkIndex": item.get("chunkIndex") or item.get("chunk_index"),
                    "section": item.get("section"),
                    "text": _safe_preview(item.get("text") or item.get("content")),
                }
                for item in (edge.get("evidence", []) or [])
                if isinstance(item, dict)
            ],
        )


class InvalidGraphPatchFragmentError(ValueError):
    """The LLM returned JSON that is not the canonical GraphPatchFragment."""

    error_kind = "invalid_graph_patch_fragment"
    retryable = True

    def __init__(self, message: str, *, summary: dict[str, Any] | None = None):
        super().__init__(message)
        self.summary = summary or {}


class BatchExtractor(Protocol):
    def extract_fragment(
        self,
        *,
        batch_payload: dict[str, Any],
        ontology_catalog: str,
        previous_error: dict[str, Any] | None = None,
        graph_context: str | None = None,
    ) -> GraphPatchFragment:
        """Return one source-grounded graph fragment for a staged batch."""

    def repair_fragment(
        self,
        *,
        validation_errors: dict[str, Any],
        affected_chunks: list[dict[str, Any]],
        ontology_catalog: str,
        graph_context: str | None = None,
        rejected_candidate_facts: dict[str, Any],
        accepted_candidate_facts: dict[str, Any],
    ) -> GraphPatchFragment:
        """Return only the facts needed to repair rejected fragment locations."""


class GeminiBatchExtractor:
    """Extract one GraphPatchFragment per batch using Gemini directly."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_INGESTION_MODEL,
        client: genai.Client | None = None,
    ):
        self.model = model
        self.client = client or genai.Client(
            api_key=os.getenv("GOOGLE_API_KEY"),
            http_options=types.HttpOptions(
                timeout=max(1, round(DEFAULT_INGESTION_TIMEOUT_SECONDS * 1000))
            ),
        )

    def extract_fragment(
        self,
        *,
        batch_payload: dict[str, Any],
        ontology_catalog: str,
        previous_error: dict[str, Any] | None = None,
        graph_context: str | None = None,
    ) -> GraphPatchFragment:
        prompt = self._prompt(
            batch_payload=batch_payload,
            ontology_catalog=ontology_catalog,
            previous_error=previous_error,
            graph_context=graph_context,
        )
        logger.info(
            "[EXTRACTION_REQUEST] batch=%s chunk_ids=%s existing_context_nodes=%s "
            "ontology_classes=%s ontology_properties=%s prompt_chars=%s",
            batch_payload.get("batchIndex"),
            batch_payload.get("chunkIndexes"),
            graph_context.count("- ref=") if graph_context else 0,
            len(set(re.findall(r"\bpskg:[A-Za-z_][A-Za-z0-9_.-]*", ontology_catalog))),
            ontology_catalog.count("property") + ontology_catalog.count("Property"),
            len(prompt),
        )
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=GraphPatchFragment.model_json_schema(),
                temperature=0,
            ),
        )
        parsed = getattr(response, "parsed", None)
        if parsed is not None:
            _log_raw_payload(
                "RAW_EXTRACTION_RESULT",
                parsed,
                batch=batch_payload.get("batchIndex"),
            )
            return self._validate_fragment(parsed)
        text = getattr(response, "text", None)
        if not text:
            raise ValueError("Gemini returned no graph fragment JSON")
        payload = self._parse_json_response(text)
        _log_raw_payload(
            "RAW_EXTRACTION_RESULT",
            payload,
            batch=batch_payload.get("batchIndex"),
        )
        return self._validate_fragment(payload)

    def repair_fragment(
        self,
        *,
        validation_errors: dict[str, Any],
        affected_chunks: list[dict[str, Any]],
        ontology_catalog: str,
        graph_context: str | None = None,
        rejected_candidate_facts: dict[str, Any],
        accepted_candidate_facts: dict[str, Any],
    ) -> GraphPatchFragment:
        prompt = self._repair_prompt(
            validation_errors=validation_errors,
            affected_chunks=affected_chunks,
            ontology_catalog=ontology_catalog,
            graph_context=graph_context,
            rejected_candidate_facts=rejected_candidate_facts,
            accepted_candidate_facts=accepted_candidate_facts,
        )
        logger.info(
            "[SEMANTIC_REPAIR_REQUEST] affected_chunks=%s existing_context_nodes=%s "
            "rejected_nodes=%s accepted_nodes=%s ontology_classes=%s prompt_chars=%s",
            [chunk.get("index") for chunk in affected_chunks],
            graph_context.count("- ref=") if graph_context else 0,
            len(rejected_candidate_facts.get("nodes", []) or []),
            len(accepted_candidate_facts.get("nodes", []) or []),
            len(set(re.findall(r"\bpskg:[A-Za-z_][A-Za-z0-9_.-]*", ontology_catalog))),
            len(prompt),
        )
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=GraphPatchFragment.model_json_schema(),
                temperature=0,
            ),
        )
        parsed = getattr(response, "parsed", None)
        if parsed is not None:
            _log_raw_payload(
                "SEMANTIC_REPAIR_RAW_RESULT",
                parsed,
                batch=None,
            )
            return self._validate_fragment(parsed)
        text = getattr(response, "text", None)
        if not text:
            raise ValueError("Gemini returned no graph repair fragment JSON")
        payload = self._parse_json_response(text)
        _log_raw_payload("SEMANTIC_REPAIR_RAW_RESULT", payload, batch=None)
        return self._validate_fragment(payload)

    @staticmethod
    def _parse_json_response(text: str) -> Any:
        payload = text.strip()
        if payload.startswith("```"):
            lines = payload.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            payload = "\n".join(lines).strip()
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise InvalidGraphPatchFragmentError(
                "LLM returned invalid GraphPatchFragment JSON: "
                f"{exc.msg} at line {exc.lineno} column {exc.colno}",
                summary={
                    "kind": "json_decode_error",
                    "message": exc.msg,
                    "line": exc.lineno,
                    "column": exc.colno,
                },
            ) from exc

    @staticmethod
    def _validate_fragment(payload: Any) -> GraphPatchFragment:
        payload = GeminiBatchExtractor._coerce_fragment_payload(payload)
        try:
            return GraphPatchFragment.model_validate(payload)
        except ValidationError as exc:
            raise InvalidGraphPatchFragmentError(
                "LLM returned invalid GraphPatchFragment structure: "
                f"{GeminiBatchExtractor._invalid_shape_summary(payload, exc)}",
                summary=GeminiBatchExtractor._invalid_shape_summary(payload, exc),
            ) from exc

    @staticmethod
    def _coerce_fragment_payload(payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        coerced = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        for collection_name in ("nodes", "edges"):
            for item in coerced.get(collection_name, []) or []:
                GeminiBatchExtractor._coerce_evidence_items(item.get("evidence"))
                for prop in item.get("properties", []) or []:
                    GeminiBatchExtractor._coerce_evidence_items(prop.get("evidence"))
        return coerced

    @staticmethod
    def _coerce_evidence_items(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            if "text" not in item and "content" in item:
                item["text"] = item["content"]
            item.pop("content", None)

    @staticmethod
    def _invalid_shape_summary(
        payload: Any,
        exc: ValidationError,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "kind": type(payload).__name__,
            "validationErrors": exc.errors(include_url=False, include_input=False)[:5],
        }
        if isinstance(payload, dict):
            summary["topLevelKeys"] = sorted(str(key) for key in payload)
            summary["forbiddenKeysPresent"] = [
                key
                for key in ("entities", "chunkStatus", "id", "class")
                if key in payload
            ]
        elif isinstance(payload, list):
            summary["length"] = len(payload)
            first = payload[0] if payload else None
            if isinstance(first, dict):
                summary["firstItemKeys"] = sorted(str(key) for key in first)
                summary["forbiddenKeysPresent"] = [
                    key
                    for key in ("entities", "chunkStatus", "id", "class")
                    if key in first
                ]
        return summary

    @staticmethod
    def _prompt(
        *,
        batch_payload: dict[str, Any],
        ontology_catalog: str,
        previous_error: dict[str, Any] | None,
        graph_context: str | None = None,
    ) -> str:
        repair_block = ""
        if previous_error is not None:
            repair_block = (
                "\nPrevious submission failed. Repair it before returning JSON.\n"
                f"{json.dumps(previous_error, ensure_ascii=False)}\n"
                "If the previous response used entities/chunkStatus or returned "
                "an array, convert it to the canonical GraphPatchFragment object "
                "with nodes/edges/coverage/warnings.\n"
            )
        graph_context_block = ""
        if graph_context:
            graph_context_block = (
                "\nExisting canonical graph from accepted earlier batches. "
                "You may create edges from candidate nodes in this batch to "
                "existing nodes using their ref; do not duplicate existing "
                "nodes just to create an edge.\n"
                f"{graph_context}\n"
            )
        return (
            "You are extracting one batch for a Product Sales Knowledge Graph.\n"
            "Your output is a candidate fragment, not authoritative graph truth. "
            "Downstream validation resolves canonical identity and relationship "
            "ownership from ontology, source evidence, and graph context. "
            "Do not force a class or edge when source scope is unclear; use "
            "warnings or AMBIGUOUS coverage instead.\n"
            "The response schema (GraphPatchFragment) is enforced: return "
            "exactly one object with nodes, edges, coverage, warnings and no "
            "extra keys.\n"
            "Process each chunk one by one.\n"
            "Coverage must contain exactly the chunkIndexes in this batch.\n"
            "Mark a chunk MAPPED only if at least one property or edge evidence "
            "item cites that exact chunkIndex.\n"
            "If a chunk has no distinct ontology-representable fact, mark it "
            "NO_RELEVANT_FACT with a source-based reason. Use DUPLICATE_EVIDENCE "
            "when the chunk repeats a fact already represented in candidate "
            "context. Use UNSUPPORTED_BY_ONTOLOGY, AMBIGUOUS, or FAILED rather "
            "than pretending a relevant incomplete fact is not relevant.\n"
            "Evidence text must be a verbatim excerpt from the cited chunk; keep "
            "Markdown table pipes and original whitespace/case.\n"
            "Use the most specific ontology class, property, and edge supported "
            "by source evidence. Do not collapse independently queryable facts "
            "into generic text properties. For list-valued properties, keep "
            "values atomic and source-grounded. For Markdown tables, copy the "
            "complete original row including leading/trailing pipe delimiters.\n"
            "Do not put customer audience or segment phrases such as 'Dành cho "
            "khách hàng cá nhân' into pskg:productAttributes. Use "
            "pskg:CustomerSegment/pskg:targetsSegment only when the source provides "
            "enough identity and relationship evidence; otherwise do not fabricate "
            "a segment code and do not coerce the phrase into productAttributes.\n"
            "Map pskg:bankingProductName only from an explicit product-name field "
            "such as | Tên sản phẩm | ... |. Never infer bankingProductName from "
            "| Tên tài liệu | ... | or from the document title.\n"
            "For product documents, prioritize explicit catalog metadata rows "
            "before feature details. When a chunk states product code, product "
            "effective date, or product name, emit those source-grounded "
            "properties on the BankingProduct candidate before adding secondary "
            "facts. A persistent product graph needs the source-supported "
            "natural identifier, effective date, and at least one required rule "
            "relationship when the source states the rule evidence.\n"
            "Do not fabricate lifecycle statuses, codes, dates, identifiers, or "
            "relationships. Omit properties whose ontology policy is "
            "runtime_managed, system_default, or edge_derived; the compiler "
            "supplies those values.\n"
            "Do not force compound or multi-dimensional facts into a scalar "
            "property when the ontology provides a richer node or relationship "
            "representation; choose the ontology representation that preserves "
            "all source-supported semantics.\n\n"
            "Honor mandatory ontology rules (RULES entries with operator=some "
            "on outgoing edges): when the source supports the relationship, "
            "emit that edge in this fragment or reference the existing "
            "canonical target; a missing mandatory edge blocks persistence. "
            "Never fabricate a relationship the source does not support.\n"
            "When ontology constraints require a missing relationship, inspect "
            "the provided source semantically for facts that satisfy the "
            "constraint. Do not rely only on lexical overlap between ontology "
            "terminology and source wording. Use definitions, section meaning "
            "and surrounding context. Emit the relationship only when source "
            "evidence supports it; otherwise do not invent it.\n\n"
            f"{graph_context_block}"
            "Ontology catalog:\n"
            f"{ontology_catalog}\n"
            f"{repair_block}\n"
            "Batch payload:\n"
            f"{json.dumps(batch_payload, ensure_ascii=False)}"
        )

    @staticmethod
    def _repair_prompt(
        *,
        validation_errors: dict[str, Any],
        affected_chunks: list[dict[str, Any]],
        ontology_catalog: str,
        graph_context: str | None,
        rejected_candidate_facts: dict[str, Any],
        accepted_candidate_facts: dict[str, Any],
    ) -> str:
        graph_context_block = ""
        if graph_context:
            graph_context_block = (
                "\nExisting canonical graph from accepted earlier batches. "
                "You may create edges from repaired candidate nodes to existing "
                "nodes using their ref; do not duplicate existing nodes just to "
                "create an edge.\n"
                f"{graph_context}\n"
            )
        return (
            "You are repairing rejected locations in one candidate "
            "GraphPatchFragment.\n"
            "Repair only the rejected candidate facts or coverage decisions "
            "identified by the validation errors.\n"
            "Preserve all accepted facts unchanged. Do not regenerate, "
            "reinterpret, replace, or remove facts that already passed "
            "validation.\n"
            "Use only source-supported evidence from the affected chunks and "
            "the ontology catalog.\n"
            "Return only facts needed to repair the rejected locations as a "
            "GraphPatchFragment. The caller will merge this repair fragment "
            "into the frozen accepted candidate and validate the full merged "
            "fragment.\n"
            "Evidence text must be a verbatim excerpt from the cited chunk; "
            "keep original whitespace/case and Markdown table delimiters. "
            "The chunk section/title and source filename are metadata only: "
            "put them in section/source, never in evidence.text unless the same "
            "text also appears inside the chunk content. If no chunk-content "
            "excerpt supports a rejected fact, omit that fact and return a "
            "coverage decision that reflects the missing support.\n\n"
            f"{graph_context_block}"
            "Ontology catalog:\n"
            f"{ontology_catalog}\n\n"
            "Validation errors:\n"
            f"{json.dumps(validation_errors, ensure_ascii=False)}\n\n"
            "Affected source chunks:\n"
            f"{json.dumps(affected_chunks, ensure_ascii=False)}\n\n"
            "Rejected candidate facts:\n"
            f"{json.dumps(rejected_candidate_facts, ensure_ascii=False)}\n\n"
            "Accepted candidate facts (read-only):\n"
            f"{json.dumps(accepted_candidate_facts, ensure_ascii=False)}"
        )


__all__ = [
    "DEFAULT_INGESTION_MODEL",
    "BatchExtractor",
    "GeminiBatchExtractor",
    "InvalidGraphPatchFragmentError",
]
