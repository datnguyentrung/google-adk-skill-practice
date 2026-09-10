"""Hàm hỗ trợ dùng chung cho các validator và judge trong phase kiểm định.

Các helper ở đây chỉ làm việc với payload thuần (dict/str) và không giữ trạng thái:
- `_node_payload`: chuẩn hoá node (draft hoặc compiled) thành dict cho LLM judge.
- `_safe_preview`: cắt ngắn text và che dữ liệu nhạy cảm trước khi ghi log.
- `_evidence_trace`: chuyển danh sách evidence thành trace gọn cho log debug.

Module này là module lá, không import các module khác trong package `validation`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.core.schemas.ingestion.graph_patch import Evidence

# Nhận diện các cặp khoá/giá trị nhạy cảm để che trước khi ghi log.
_SENSITIVE_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*\S+|api[_-]?key\s*[:=]\s*\S+|"
    r"token\s*[:=]\s*\S+|password\s*[:=]\s*\S+|pin\s*[:=]\s*\S+|"
    r"otp\s*[:=]\s*\S+)"
)


def _node_payload(node: Any) -> dict[str, Any]:
    """Chuyển node (ExtractedNode hoặc CompiledNode) thành payload JSON cho judge.

    Args:
        node: Node cần mô tả, có thể là pydantic model hoặc dataclass.

    Returns:
        Dict gồm `tempId`, `className`, `properties`. Node có `properties` dạng
        danh sách entry sẽ được quy đổi thành dict tên thuộc tính → giá trị.
    """

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


def _safe_preview(value: Any, *, limit: int = 500) -> str:
    """Tạo bản xem trước một dòng, đã che thông tin nhạy cảm, dùng cho log.

    Args:
        value: Giá trị bất kỳ; giá trị không phải str sẽ được `repr()`.
        limit: Độ dài tối đa; vượt ngưỡng sẽ cắt giữa và nối bằng `...`.

    Returns:
        Chuỗi một dòng đã chuẩn hoá khoảng trắng và che token mật.
    """

    text = value if isinstance(value, str) else repr(value)
    text = _SENSITIVE_PATTERN.sub("<REDACTED>", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return f"{text[:half]} ... {text[-half:]}"


def _evidence_trace(evidence_items: Iterable[Evidence]) -> list[dict[str, Any]]:
    """Chuyển danh sách evidence thành trace ngắn gọn (text đã cắt/che) cho log.

    Args:
        evidence_items: Các evidence cần ghi vết.

    Returns:
        Danh sách dict gồm `chunkIndex`, `source`, `section`, `text`.
    """

    return [
        {
            "chunkIndex": item.chunk_index,
            "source": item.source,
            "section": item.section,
            "text": _safe_preview(item.text),
        }
        for item in evidence_items
    ]
