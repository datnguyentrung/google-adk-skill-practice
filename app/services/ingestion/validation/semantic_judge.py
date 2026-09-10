"""Phase kiểm định — tầng phán xử ngữ nghĩa (semantic judge).

Validator tất định chỉ so khớp chuỗi/số/datatype nên không xử lý được các trường
hợp diễn đạt lại (paraphrase). Module này bọc một LLM (Gemini) sau một interface
nhỏ để trả lời hai câu hỏi:
- `judge_edge`: evidence có thật sự nêu quan hệ giữa hai node không?
- `judge_value`: evidence có thật sự chống đỡ mọi claim trong một giá trị không?

Khi không cấu hình LLM, `PermissiveSemanticGroundingJudge` được dùng làm fallback
để không chặn oan (vì các kiểm tra tất định đã qua).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.core.schemas.ingestion.graph_patch import Evidence
from app.services.ingestion.mapping.model_call import AdkStructuredCallExecutor
from app.services.ingestion.ontology.registry import OntologyRegistry
from app.services.ingestion.validation.support import _node_payload

logger = logging.getLogger(__name__)

# Model dùng cho semantic judge: ưu tiên biến môi trường riêng của judge, sau đó
# tới orchestrator, cuối cùng là model ADK mặc định của app.
DEFAULT_SEMANTIC_GROUNDING_MODEL = os.getenv(
    "INGESTION_SEMANTIC_GROUNDING_MODEL",
    os.getenv(
        "INGESTION_ORCHESTRATOR_MODEL",
        os.getenv("GOOGLE_ADK_MODEL", "gemini-3.5-flash-lite"),
    ),
)


@dataclass(frozen=True)
class SemanticGroundingDecision:
    """Kết quả phán xử: `supported` / `unsupported` / `unknown` kèm lý do."""

    verdict: Literal["supported", "unsupported", "unknown"]
    reason: str = ""
    missing_evidence: tuple[str, ...] = ()


class SemanticGroundingJudge(Protocol):
    """Interface phán xử quan hệ: caller chỉ cần biết `judge_edge`."""

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
    """Interface phán xử giá trị thuộc tính: caller chỉ cần biết `judge_value`."""

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
        """Luôn trả `supported` vì chưa có judge LLM nào được cấu hình."""

        return SemanticGroundingDecision(
            verdict="supported",
            reason="Semantic grounding judge is not configured; deterministic checks passed.",
        )


class _JudgeResponse(BaseModel):
    """Schema JSON mà LLM judge bắt buộc phải trả về."""

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
        structured_executor: AdkStructuredCallExecutor | None = None,
    ):
        """Khởi tạo judge, dùng lại executor có sẵn hoặc tạo mới theo `model`.

        Args:
            model: Tên model Gemini dùng để phán xử.
            structured_executor: Executor gọi model có ràng buộc JSON; bỏ trống
                thì tự tạo mới.
        """

        self.model = model
        self.structured_executor = structured_executor or AdkStructuredCallExecutor(
            model=self.model
        )
        # Cache theo payload để không gọi lại model cho cùng một câu hỏi.
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
        """Phán xử một edge, có cache theo nội dung payload.

        Args:
            edge: Edge cần phán xử (có `edge_name` và tempId hai đầu).
            source_node: Node nguồn đã resolve.
            target_node: Node đích đã resolve.
            evidence_items: Evidence mà edge viện dẫn.
            registry: Registry ontology để lấy định nghĩa edge.

        Returns:
            `SemanticGroundingDecision`; lỗi gọi model được hạ thành `unknown`.
        """

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
        decision = self._run_judge(
            operation="semantic_grounding_edge",
            prompt=self._prompt(payload),
            log_context=f"edge_name={edge.edge_name}",
        )
        self._cache[key] = decision
        return decision

    @staticmethod
    def _prompt(payload: dict[str, Any]) -> str:
        """Dựng prompt phán xử quan hệ; giữ nguyên văn bản gốc để không đổi hành vi."""

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
        """Dựng payload mô tả edge + hai node + evidence để gửi cho model."""

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
        """Phán xử một giá trị thuộc tính, có cache theo nội dung payload.

        Args:
            property_name: Tên kỹ thuật của property.
            value: Giá trị LLM trích xuất (có thể chứa nhiều claim).
            evidence_items: Evidence mà property viện dẫn.
            attribute: Định nghĩa attribute trong ontology (có thể là None).

        Returns:
            `SemanticGroundingDecision`; lỗi gọi model được hạ thành `unknown`.
        """

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
        decision = self._run_judge(
            operation="semantic_grounding_value",
            prompt=self._value_prompt(payload),
            log_context=f"property={property_name}",
        )
        self._cache[key] = decision
        return decision

    def _run_judge(
        self,
        *,
        operation: str,
        prompt: str,
        log_context: str,
    ) -> SemanticGroundingDecision:
        """Gọi model có ràng buộc JSON rồi quy đổi kết quả về decision.

        Args:
            operation: Tên thao tác dùng cho log/đo lường của executor.
            prompt: Prompt đã dựng sẵn.
            log_context: Ngữ cảnh ngắn để ghi log khi lỗi.

        Returns:
            Decision hợp lệ; mọi exception được nuốt và trả verdict `unknown`
            để lỗi hạ tầng không bị biến thành kết luận "unsupported".
        """

        try:
            payload = self.structured_executor.run(
                operation=operation,
                instruction=prompt,
                output_schema=_JudgeResponse.model_json_schema(by_alias=True),
                message="Judge the supplied evidence and return the structured verdict.",
            )
            model_response = _JudgeResponse.model_validate(payload)
            return SemanticGroundingDecision(
                verdict=model_response.verdict,
                reason=model_response.reason,
                missing_evidence=tuple(model_response.missing_evidence or []),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Semantic grounding judge failed %s error=%s", log_context, exc)
            return SemanticGroundingDecision(
                verdict="unknown",
                reason=f"Semantic grounding judge failed: {exc}",
            )

    @staticmethod
    def _value_prompt(payload: dict[str, Any]) -> str:
        """Dựng prompt phán xử giá trị; giữ nguyên văn bản gốc để không đổi hành vi."""

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
        """Dựng payload mô tả property + giá trị + evidence để gửi cho model."""

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
    """Chọn judge mặc định cho edge: Gemini nếu được bật rõ ràng, ngược lại permissive.

    Returns:
        `GeminiSemanticGroundingJudge` khi `INGESTION_SEMANTIC_GROUNDING_JUDGE=gemini`
        và có `GOOGLE_API_KEY`; các trường hợp còn lại dùng judge permissive.
    """

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
