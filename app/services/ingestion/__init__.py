"""Pipeline ingestion tài liệu thành Knowledge Graph (Product Sales).

Hệ thống hoạt động theo mô hình Skill-driven (LLM Ingestion Skill):
SKILL.md sở hữu luồng điều phối (orchestration), chọn schema, trích xuất và retry.
Python cung cấp các primitive tools deterministic phục vụ Skill:

- `ontology/`     — Phase 0: nạp và tra cứu ontology.
- `document/`     — Phase 1: đọc tài liệu và cắt chunk.
- `workspace/`    — Phase 2: chia batch và gộp fragment.
- `patch/`        — Phase 3: biên dịch và chốt chặn fragment.
- `validation/`   — Phase 4: kiểm định patch & persistent staging.
- `persistence/`  — Phase 5: ghi Neo4j & staging persistence.
- `incremental/`  — Persistent staging & identity resolution.
- `orchestration/` — Session state, artifact prep & deterministic helpers.
- `identity/`     — Định danh node (dùng chung cho validation & persistence).

Package này không import sẵn bất kỳ module con nào để tránh kéo theo các phụ thuộc
nặng (neo4j, google-adk) khi chỉ cần một phần nhỏ của pipeline.
"""

__all__ = [
    "document",
    "identity",
    "incremental",
    "ontology",
    "orchestration",
    "patch",
    "persistence",
    "validation",
    "workspace",
]
