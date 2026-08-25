from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol

from google import genai
from google.genai import types
from pydantic import ValidationError

from app.core.schemas.ingestion.graph_patch import GraphPatchFragment

logger = logging.getLogger(__name__)

DEFAULT_INGESTION_MODEL = os.getenv(
    "INGESTION_ORCHESTRATOR_MODEL",
    os.getenv("GOOGLE_ADK_MODEL", "gemini-3.1-flash-lite"),
)


CANONICAL_FRAGMENT_CONTRACT = """
Canonical GraphPatchFragment JSON contract:
- Return one JSON object, never an array.
- Top-level keys must be exactly: nodes, edges, coverage, warnings.
- Forbidden keys anywhere in the response: entities, chunkStatus, id, class.
- nodes is an array of objects with exactly:
  tempId, className, properties, evidence, confidence.
- node.properties is always an array, never a property map/object. Each item has:
  propertyName, value, evidence.
- edges is an array of objects with exactly:
  edgeName, sourceTempId, targetTempId, evidence, confidence.
- coverage is an array of objects with exactly:
  chunkIndex, decision, reason.
- evidence is an array of objects with:
  source, chunkIndex, section, text.

Minimal valid empty-fact batch:
{
  "nodes": [],
  "edges": [],
  "coverage": [
    {"chunkIndex": 0, "decision": "NOT_RELEVANT", "reason": "No distinct ontology-representable business fact in this chunk"}
  ],
  "warnings": []
}

Valid mapped property example:
{
  "nodes": [{
    "tempId": "product-cc-flexi-001",
    "className": "pskg:BankingProduct",
    "properties": [{
      "propertyName": "pskg:productCode",
      "value": "CC-FLEXI-001",
      "evidence": [{
        "source": "example.md",
        "chunkIndex": 1,
        "section": "Product information",
        "text": "| Mã sản phẩm | CC-FLEXI-001 |"
      }]
    }],
    "evidence": [{
      "source": "example.md",
      "chunkIndex": 1,
      "section": "Product information",
      "text": "| Mã sản phẩm | CC-FLEXI-001 |"
    }],
    "confidence": 0.98
  }],
  "edges": [],
  "coverage": [
    {"chunkIndex": 1, "decision": "MAPPED", "reason": "Contains product code"}
  ],
  "warnings": []
}
""".strip()


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
    ) -> GraphPatchFragment:
        """Return one source-grounded graph fragment for a staged batch."""


class GeminiBatchExtractor:
    """Extract one GraphPatchFragment per batch using Gemini directly."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_INGESTION_MODEL,
        client: genai.Client | None = None,
    ):
        self.model = model
        self.client = client or genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

    def extract_fragment(
        self,
        *,
        batch_payload: dict[str, Any],
        ontology_catalog: str,
        previous_error: dict[str, Any] | None = None,
    ) -> GraphPatchFragment:
        prompt = self._prompt(
            batch_payload=batch_payload,
            ontology_catalog=ontology_catalog,
            previous_error=previous_error,
        )
        logger.info(
            "ORCHESTRATOR_LLM_REQUEST batch=%s chunks=%s",
            batch_payload.get("batchIndex"),
            batch_payload.get("chunkIndexes"),
        )
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0,
            ),
        )
        parsed = getattr(response, "parsed", None)
        if parsed is not None:
            return self._validate_fragment(parsed)
        text = getattr(response, "text", None)
        if not text:
            raise ValueError("Gemini returned no graph fragment JSON")
        return self._validate_fragment(self._parse_json_response(text))

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
        try:
            return GraphPatchFragment.model_validate(payload)
        except ValidationError as exc:
            raise InvalidGraphPatchFragmentError(
                "LLM returned invalid GraphPatchFragment structure: "
                f"{GeminiBatchExtractor._invalid_shape_summary(payload, exc)}",
                summary=GeminiBatchExtractor._invalid_shape_summary(payload, exc),
            ) from exc

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
        return (
            "You are extracting one batch for a Product Sales Knowledge Graph.\n"
            "Return only valid JSON matching the canonical GraphPatchFragment "
            "contract below.\n"
            f"{CANONICAL_FRAGMENT_CONTRACT}\n\n"
            "Process each chunk one by one.\n"
            "Coverage must contain exactly the chunkIndexes in this batch.\n"
            "Mark a chunk MAPPED only if at least one property or edge evidence "
            "item cites that exact chunkIndex.\n"
            "If a chunk has no distinct ontology-representable fact, mark it "
            "NOT_RELEVANT with a source-based reason.\n"
            "Evidence text must be a verbatim excerpt from the cited chunk; keep "
            "Markdown table pipes and original whitespace/case.\n"
            "For pskg:productAttributes, always emit a JSON list, even when "
            "there is only one value. Use it only for product-specific attributes "
            "such as card tier, currency, channel, or capability; keep values atomic "
            "and source-grounded. Every list item must be supported by at least one "
            "verbatim evidence row. For Markdown tables, copy the complete original "
            "row including leading/trailing pipe delimiters. Never synthesize a "
            "semicolon-joined evidence sentence from multiple rows.\n"
            "Do not put customer audience or segment phrases such as 'Dành cho "
            "khách hàng cá nhân' into pskg:productAttributes. Use "
            "pskg:CustomerSegment/pskg:targetsSegment only when the source provides "
            "enough identity and relationship evidence; otherwise do not fabricate "
            "a segment code and do not coerce the phrase into productAttributes.\n"
            "Map pskg:bankingProductName only from an explicit product-name field "
            "such as | Tên sản phẩm | ... |. Never infer bankingProductName from "
            "| Tên tài liệu | ... | or from the document title.\n"
            "Do not stuff independent fee, interest, penalty, annual-fee, "
            "withdrawal-charge, late-payment, over-limit, or pricing facts into "
            "scalar BankingProduct.pskg:fee. When the ontology supports only "
            "BusinessRule granularity, create one pskg:BusinessRule per distinct "
            "fee/pricing fact, put the fee type, amount/rate, and condition in "
            "pskg:businessRuleCondition, and connect the product with "
            "pskg:hasSalesConditionRule. For every BusinessRule, emit each "
            "propertyName at most once. If one semantic rule has multiple grounded "
            "conditions, emit one pskg:businessRuleCondition property whose value "
            "is a JSON list. If the conditions are distinct rules, use distinct "
            "tempIds instead of repeating a property on one node. "
            "Do not emit pskg:ruleType directly; "
            "the compiler derives SALES_CONDITION from that edge.\n"
            "Do not fabricate facts. Do not emit pskg:ruleType directly.\n\n"
            "Ontology catalog:\n"
            f"{ontology_catalog}\n"
            f"{repair_block}\n"
            "Batch payload:\n"
            f"{json.dumps(batch_payload, ensure_ascii=False)}"
        )


__all__ = [
    "BatchExtractor",
    "DEFAULT_INGESTION_MODEL",
    "GeminiBatchExtractor",
    "InvalidGraphPatchFragmentError",
]
