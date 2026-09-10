"""Chính sách định danh (identity policy) cho Product Sales Knowledge Graph.

Khai báo natural key — thuộc tính dùng làm định danh nghiệp vụ cho từng class.
Đây là operational policy của ingestion skill, KHÔNG sửa ontology.json, và chỉ
khai báo natural key khi thật sự có identifier/code đủ rõ."""


PRODUCT_SALES_NATURAL_KEYS: dict[str, str] = {
    "pskg:BankingProduct": "pskg:productCode",
    "pskg:ProductCategory": "pskg:categoryCode",
    "pskg:CustomerSegment": "pskg:segmentCode",
    "pskg:CustomerNeed": "pskg:needCode",
    "pskg:ProductBundle": "pskg:bundleCode",
    "pskg:ApprovalTask": "pskg:approvalTaskId",
    "pskg:Customer": "pskg:customerIdentifier",
}
