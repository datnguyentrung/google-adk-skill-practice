"""Pipeline ingestion tài liệu thành Knowledge Graph (Product Sales).

Package này tổ chức toàn bộ pipeline theo phase, mỗi phase là một package con có
interface riêng (xem `README.md` để biết sơ đồ đầy đủ):

- `ontology/`   — Phase 0: nạp và tra cứu ontology.
- `document/`   — Phase 1: đọc tài liệu và chuẩn bị ngữ cảnh extraction.
- `mapping/`    — Phase 2: gọi LLM trích xuất graph patch.
- `workspace/`  — Phase 2b: chia batch và quản lý workspace theo phiên.
- `patch/`      — Phase 3: biên dịch và chốt chặn fragment.
- `validation/` — Phase 4: kiểm định patch trước khi ghi.
- `persistence/`— Phase 5: ghi Neo4j và đọc lại để xác minh.
- `orchestration/` — Phase 6: điều phối end-to-end và các bước tool/agent.
- `identity/`   — định danh node (dùng chung cho nhiều phase).

Package này không import sẵn bất kỳ module con nào để tránh kéo theo các phụ thuộc
nặng (neo4j, google-adk) khi chỉ cần một phần nhỏ của pipeline.
"""

__all__ = [
    "document",
    "identity",
    "mapping",
    "ontology",
    "orchestration",
    "patch",
    "persistence",
    "validation",
    "workspace",
]
