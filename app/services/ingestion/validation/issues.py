"""Tiện ích xử lý ValidationIssue — nền tảng chung cho phase kiểm định.

Module này gom các hàm thuần (không phụ thuộc registry hay LLM) dùng để:
- khử trùng lặp danh sách issue trước khi trả về cho caller;
- đánh giá ràng buộc số lượng (cardinality) của rule ontology.

Đây là module lá của package `validation`: các module khác trong package đều có
thể import, nhưng module này không import ngược lại nên tránh được circular import.
"""

from __future__ import annotations

from typing import Any

from app.core.schemas.ingestion.validation import ValidationIssue


def deduplicate_issues(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    """Khử issue trùng theo bộ khoá (code, location, message).

    Args:
        issues: Danh sách issue thô, có thể chứa nhiều bản ghi giống hệt nhau do
            nhiều nhánh kiểm tra cùng phát hiện một lỗi.

    Returns:
        Danh sách issue đã loại trùng, giữ thứ tự của lần xuất hiện đầu tiên.
    """

    unique = {(str(issue.code), issue.location, issue.message): issue for issue in issues}
    return list(unique.values())


def cardinality_failure(
    operator: str, raw_expected: Any, actual_count: int
) -> tuple[str, int] | None:
    """Quy đổi rule cardinality của ontology thành mô tả lỗi (kind, số kỳ vọng).

    Args:
        operator: Toán tử rule (`some`, `exactlyQualified`, `minQualified`).
        raw_expected: Giá trị kỳ vọng thô lấy từ ontology (có thể là str/int).
        actual_count: Số lượng thực tế đếm được trên graph patch.

    Returns:
        `None` nếu rule được thoả; ngược lại trả `("minimum", n)` hoặc
        `("exactly", n)` để caller dựng message lỗi.
    """

    if operator == "some":
        return ("minimum", 1) if actual_count < 1 else None
    try:
        expected = int(raw_expected)
    except (TypeError, ValueError):
        return None
    if operator == "exactlyQualified" and actual_count != expected:
        return ("exactly", expected)
    if operator == "minQualified" and actual_count < expected:
        return ("minimum", expected)
    return None
