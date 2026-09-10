"""Phase 2 — Sinh và chuẩn hoá tempId cho node/edge theo từng batch.

LLM đặt tempId tự do nên có thể trùng hoặc lệch giữa các batch. Các hàm ở đây gắn
tiền tố batch, bỏ tiền tố lặp và tạo giá trị ổn định để tempId trở thành duy nhất
trong toàn bộ tài liệu trước khi gộp fragment."""

import json
import re
from typing import Any


def _batch_prefix(batch_index: int) -> str:
    """
    Sinh tiền tố tempId cho một batch (ví dụ `b3`).
    """
    return f"b{batch_index}__"


def _strip_repeated_batch_prefix(temp_id: str, batch_index: int) -> str:
    """
    Bỏ tiền tố batch bị lặp nhiều lần trong tempId do LLM sinh.
    """
    prefix = _batch_prefix(batch_index)
    while temp_id.startswith(prefix):
        temp_id = temp_id[len(prefix) :]
    return temp_id


def _batch_scoped_temp_id(temp_id: str, batch_index: int) -> str:
    """
    Ghép tempId với tiền tố batch để bảo đảm duy nhất toàn tài liệu.
    """
    return (
        f"{_batch_prefix(batch_index)}"
        f"{_strip_repeated_batch_prefix(temp_id, batch_index)}"
    )


def _short_definition(value: str) -> str:
    """
    Rút gọn phần định nghĩa dài trước khi đưa vào prompt.
    """
    text = str(value or "").split("[Business constraint]", 1)[0].strip()
    return re.sub(r"\s+", " ", text)


def _stable_value(value: Any) -> str:
    """
    Chuỗi hoá giá trị theo cách ổn định (dùng khi so sánh/băm).
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
