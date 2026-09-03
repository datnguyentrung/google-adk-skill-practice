from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from google import genai
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.schemas.ingestion.graph_patch import Evidence
from app.services.ingestion.registry import OntologyRegistry

logger = logging.getLogger(__name__)

DEFAULT_SEMANTIC_GROUNDING_MODEL = os.getenv(
    "INGESTION_SEMANTIC_GROUNDING_MODEL",
    os.getenv("INGESTION_ORCHESTRATOR_MODEL", os.getenv("GOOGLE_ADK_MODEL", "gemini-3.1-flash-lite")),
)


@dataclass(frozen=True)
class SemanticGroundingDecision:
    verdict: Literal["supported", "unsupported", "unknown"]
    reason: str = ""
    missing_evidence: tuple[str, ...] = ()


class SemanticGroundingJudge(Protocol):
    def judge_edge(
        self,
        *,
        edge: Any,
        source_node: Any,
        target_node: Any,
        evidence_items: list[Evidence],
        registry: OntologyRegistry,
    ) -> SemanticGroundingDecision:
        """Decide whether evidence semantically supports an ontology edge."""


class SemanticValueJudge(Protocol):
    def judge_value(
        self,
        *,
        property_name: str,
        value: Any,
        evidence_items: list[Evidence],
        attribute: Any,
    ) -> SemanticGroundingDecision:
        """Decide whether evidence semantically supports a property value."""


class PermissiveSemanticGroundingJudge:
    """Local fallback: never blocks semantics when no LLM judge is configured."""

    def judge_edge(
        self,
        *,
        edge: Any,
        source_node: Any,
        target_node: Any,
        evidence_items: list[Evidence],
        registry: OntologyRegistry,
    ) -> SemanticGroundingDecision:
        return SemanticGroundingDecision(
            verdict="supported",
            reason="Semantic grounding judge is not configured; deterministic checks passed.",
        )


class _JudgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["supported", "unsupported", "unknown"]
    reason: str = Field(default="")
    missing_evidence: list[str] | None = Field(default=None, alias="missingEvidence")


class GeminiSemanticGroundingJudge:
    """LLM-backed semantic grounding without per-edge keyword functions."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_SEMANTIC_GROUNDING_MODEL,
        client: genai.Client | None = None,
    ):
        self.model = model
        self.client = client or genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        self._cache: dict[str, SemanticGroundingDecision] = {}

    def judge_edge(
        self,
        *,
        edge: Any,
        source_node: Any,
        target_node: Any,
        evidence_items: list[Evidence],
        registry: OntologyRegistry,
    ) -> SemanticGroundingDecision:
        payload = self._payload(
            edge=edge,
            source_node=source_node,
            target_node=target_node,
            evidence_items=evidence_items,
            registry=registry,
        )
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        if key in self._cache:
            return self._cache[key]
        try:
            from google.genai import types

            response = self.client.models.generate_content(
                model=self.model,
                contents=self._prompt(payload),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0,
                ),
            )
            parsed = getattr(response, "parsed", None)
            raw = parsed if parsed is not None else json.loads(getattr(response, "text", "") or "{}")
            model_response = _JudgeResponse.model_validate(raw)
            decision = SemanticGroundingDecision(
                verdict=model_response.verdict,
                reason=model_response.reason,
                missing_evidence=tuple(model_response.missing_evidence or []),
            )
        except (json.JSONDecodeError, ValidationError, Exception) as exc:  # noqa: BLE001
            logger.warning(
                "Semantic grounding judge failed edge_name=%s error=%s",
                edge.edge_name,
                exc,
            )
            decision = SemanticGroundingDecision(
                verdict="unknown",
                reason=f"Semantic grounding judge failed: {exc}",
            )
        self._cache[key] = decision
        return decision

    @staticmethod
    def _prompt(payload: dict[str, Any]) -> str:
        return (
            "You judge whether cited source evidence semantically supports one "
            "Product Sales Knowledge Graph edge. Use reasoning over the source "
            "language; do not require ontology labels or English keywords to appear. "
            "Return only JSON with keys verdict, reason, missingEvidence. "
            "Use verdict='supported' when the evidence, node facts, and ontology "
            "definition make the relationship directly stated or unambiguously "
            "entailed. Use verdict='unsupported' only when evidence contradicts or "
            "does not identify the relationship. Use verdict='unknown' for model "
            "or evidence ambiguity that should not be converted into a hard-coded "
            "keyword failure.\n\n"
            f"{json.dumps(payload, ensure_ascii=False, default=str)}"
        )

    @staticmethod
    def _payload(
        *,
        edge: Any,
        source_node: Any,
        target_node: Any,
        evidence_items: list[Evidence],
        registry: OntologyRegistry,
    ) -> dict[str, Any]:
        ontology_edge = registry.get_edge(edge.edge_name)
        return {
            "edge": {
                "edgeName": edge.edge_name,
                "sourceTempId": edge.source_temp_id,
                "targetTempId": edge.target_temp_id,
            },
            "ontologyEdge": (
                None
                if ontology_edge is None
                else {
                    "technicalName": ontology_edge.technical_name,
                    "label": ontology_edge.label,
                    "definition": ontology_edge.definition,
                    "domain": ontology_edge.domain,
                    "range": ontology_edge.range,
                }
            ),
            "sourceNode": _node_payload(source_node),
            "targetNode": _node_payload(target_node),
            "evidence": [
                {
                    "source": item.source,
                    "chunkIndex": item.chunk_index,
                    "section": item.section,
                    "text": item.text,
                }
                for item in evidence_items
            ],
        }


    def judge_value(
        self,
        *,
        property_name: str,
        value: Any,
        evidence_items: list[Evidence],
        attribute: Any,
    ) -> SemanticGroundingDecision:
        payload = self._value_payload(
            property_name=property_name,
            value=value,
            evidence_items=evidence_items,
            attribute=attribute,
        )
        key = "value:" + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if key in self._cache:
            return self._cache[key]
        try:
            from google.genai import types

            response = self.client.models.generate_content(
                model=self.model,
                contents=self._value_prompt(payload),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0,
                ),
            )
            parsed = getattr(response, "parsed", None)
            raw = parsed if parsed is not None else json.loads(getattr(response, "text", "") or "{}")
            model_response = _JudgeResponse.model_validate(raw)
            decision = SemanticGroundingDecision(
                verdict=model_response.verdict,
                reason=model_response.reason,
                missing_evidence=tuple(model_response.missing_evidence or []),
            )
        except (json.JSONDecodeError, ValidationError, Exception) as exc:  # noqa: BLE001
            logger.warning(
                "Semantic grounding judge failed property=%s error=%s",
                property_name,
                exc,
            )
            decision = SemanticGroundingDecision(
                verdict="unknown",
                reason=f"Semantic grounding judge failed: {exc}",
            )
        self._cache[key] = decision
        return decision

    @staticmethod
    def _value_prompt(payload: dict[str, Any]) -> str:
        return (
            "You judge whether cited source evidence semantically supports one "
            "Product Sales Knowledge Graph property value. The value may contain "
            "multiple claims; you must evaluate EVERY claim in the value against "
            "the ENTIRE evidence list. Return only JSON with keys verdict, reason, "
            "missingEvidence. Use verdict='supported' only when every claim is "
            "directly stated or unambiguously entailed by the evidence, including "
            "reasonable normalization of number, currency, date, and percent "
            "formats. Use verdict='unsupported' when any claim is missing, "
            "contradicted, or only weakly related; numeric or keyword overlap "
            "alone is NOT sufficient. Use verdict='unknown' for model or evidence "
            "ambiguity that must not be accepted as grounded. In missingEvidence, "
            "list each claim that the evidence does not support.\n\n"
            f"{json.dumps(payload, ensure_ascii=False, default=str)}"
        )

    @staticmethod
    def _value_payload(
        *,
        property_name: str,
        value: Any,
        evidence_items: list[Evidence],
        attribute: Any,
    ) -> dict[str, Any]:
        attribute_payload = None
        if attribute is not None:
            policy = getattr(attribute, "ingestion_policy", None)
            attribute_payload = {
                "technicalName": getattr(attribute, "technical_name", None),
                "label": getattr(attribute, "label", None),
                "definition": getattr(attribute, "definition", None),
                "range": getattr(attribute, "range", None),
                "grounding": getattr(policy, "grounding", None),
            }
        return {
            "property": property_name,
            "value": value,
            "ontologyAttribute": attribute_payload,
            "evidence": [
                {
                    "source": item.source,
                    "chunkIndex": item.chunk_index,
                    "section": item.section,
                    "text": item.text,
                }
                for item in evidence_items
            ],
        }


def create_default_semantic_grounding_judge() -> SemanticGroundingJudge:
    if (
        os.getenv("INGESTION_SEMANTIC_GROUNDING_JUDGE", "").casefold()
        == "gemini"
        and os.getenv("GOOGLE_API_KEY")
    ):
        return GeminiSemanticGroundingJudge()
    return PermissiveSemanticGroundingJudge()


def create_default_semantic_value_judge() -> SemanticValueJudge | None:
    """Return a Gemini value judge by default when a key exists; else fail closed."""

    if os.getenv("GOOGLE_API_KEY"):
        return GeminiSemanticGroundingJudge()
    return None


def _node_payload(node: Any) -> dict[str, Any]:
    properties = getattr(node, "properties", {})
    if isinstance(properties, dict):
        props = properties
    else:
        props = {
            item.property_name: item.value
            for item in properties
            if hasattr(item, "property_name")
        }
    return {
        "tempId": getattr(node, "temp_id", None),
        "className": getattr(node, "class_name", None),
        "properties": props,
    }


__all__ = [
    "GeminiSemanticGroundingJudge",
    "PermissiveSemanticGroundingJudge",
    "SemanticGroundingDecision",
    "SemanticGroundingJudge",
    "SemanticValueJudge",
    "create_default_semantic_grounding_judge",
    "create_default_semantic_value_judge",
]
