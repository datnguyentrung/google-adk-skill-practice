"""Schema Router — Heuristic routing of documents and batches to relevant Schema Skills."""

import logging
import re
from typing import Any

from app.core.schemas.ingestion.document import DocumentChunk
from app.services.ingestion.schema.skill_registry import (
    SKILL_DESCRIPTIONS,
    SchemaSkillRegistry,
    get_schema_skill_registry,
)

logger = logging.getLogger(__name__)


# Keywords for heuristic matching per skill ID
SKILL_KEYWORDS: dict[str, list[str]] = {
    "business-rules": [
        "rule", "policy", "eligibility", "condition", "requirement", "required document",
        "priority", "qualification", "discretion", "criteria", "terms", "quy định",
        "điều kiện", "giấy tờ", "hồ sơ", "chính sách"
    ],
    "product-catalog": [
        "product", "offer", "bundle", "card", "loan", "account", "deposit", "fee",
        "interest", "rate", "price", "benefit", "category", "sản phẩm", "gói", "thẻ",
        "vay", "tài khoản", "lãi suất", "phí"
    ],
    "governance-versioning": [
        "version", "approval", "approver", "task", "audit", "status", "published",
        "effective date", "phiên bản", "phê duyệt", "ngày hiệu lực"
    ],
    "campaign-targeting": [
        "campaign", "target", "segment", "promotion", "incentive", "discount", "reward",
        "chiến dịch", "khuyến mại", "ưu đãi", "phân khúc"
    ],
    "customer-recommendation": [
        "customer", "client", "recommend", "need", "profile", "behavior", "segment",
        "khách hàng", "nhu cầu", "gợi ý"
    ],
    "sales-enablement": [
        "script", "pitch", "knowledge", "skill", "objection", "faq", "q&a", "playbook",
        "guide", "kịch bản", "tư vấn", "hướng dẫn", "hỏi đáp"
    ],
}


class SchemaRouter:
    """Quyết định schema skill nào phù hợp cho tài liệu và từng batch."""

    def __init__(self, registry: SchemaSkillRegistry | None = None):
        self.registry = registry or get_schema_skill_registry()

    def select_for_document(
        self,
        chunks: list[DocumentChunk],
        all_skill_ids: list[str] | None = None,
    ) -> list[str]:
        """Chọn các candidate skill IDs cho toàn bộ tài liệu."""
        skills_to_check = all_skill_ids or self.registry.list_skills()
        full_text = " ".join(
            f"{c.section or ''} {c.content}".lower() for c in chunks
        )

        matched: list[str] = []
        for skill_id in skills_to_check:
            keywords = SKILL_KEYWORDS.get(skill_id, [])
            if any(re.search(r"\b" + re.escape(kw) + r"\b", full_text) for kw in keywords):
                matched.append(skill_id)

        if not matched:
            logger.warning(
                "[SCHEMA_ROUTER_NO_DOCUMENT_MATCH] No schema skill matched document text. Fallback to all candidate skills: %s",
                skills_to_check,
            )
            return list(skills_to_check)

        logger.info(
            "[SCHEMA_ROUTER_DOCUMENT_SELECTED] Selected candidate skills for document: %s",
            matched,
        )
        return matched

    def select_for_batch(
        self,
        batch_chunks: list[DocumentChunk],
        candidate_skill_ids: list[str],
    ) -> tuple[list[str], list[str]]:
        """Chọn active schema skills cho một batch cụ thể."""
        if not candidate_skill_ids:
            candidate_skill_ids = self.registry.list_skills()

        batch_text = " ".join(
            f"{c.section or ''} {c.content}".lower() for c in batch_chunks
        )

        matched: list[str] = []
        reasons: list[str] = []

        for skill_id in candidate_skill_ids:
            keywords = SKILL_KEYWORDS.get(skill_id, [])
            matched_kws = [
                kw for kw in keywords
                if re.search(r"\b" + re.escape(kw) + r"\b", batch_text)
            ]
            if matched_kws:
                matched.append(skill_id)
                reasons.append(
                    f"Skill '{skill_id}' matched keywords: {matched_kws[:3]}"
                )

        if not matched:
            logger.warning(
                "[SCHEMA_ROUTER_NO_BATCH_MATCH] No keyword match in batch chunks. Fallback to candidate skills: %s",
                candidate_skill_ids,
            )
            return list(candidate_skill_ids), [
                "Fallback: No keyword match in batch, using all candidate skills"
            ]

        logger.info(
            "[SCHEMA_ROUTER_BATCH_SELECTED] Selected skills for batch: %s | Reasons: %s",
            matched,
            reasons,
        )
        return matched, reasons

    def select_for_retry(
        self,
        batch_chunks: list[DocumentChunk],
        previous_skill_ids: list[str],
        previous_error: dict[str, Any] | None,
        candidate_skill_ids: list[str],
    ) -> tuple[list[str], list[str]]:
        """Mở rộng schema skills khi retry batch do thiếu/lỗi schema."""
        all_skills = self.registry.list_skills()
        remaining_candidates = [
            s for s in candidate_skill_ids if s not in previous_skill_ids
        ]

        if remaining_candidates:
            expanded = previous_skill_ids + remaining_candidates
            reason = [
                f"Retry expansion: added remaining candidate skills {remaining_candidates}"
            ]
        else:
            remaining_all = [s for s in all_skills if s not in previous_skill_ids]
            expanded = previous_skill_ids + remaining_all
            reason = [
                f"Retry fallback expansion: added all remaining registry skills {remaining_all}"
            ]

        logger.info(
            "[SCHEMA_ROUTER_RETRY_EXPANDED] Retry expanded schema skills from %s to %s",
            previous_skill_ids,
            expanded,
        )
        return expanded, reason
