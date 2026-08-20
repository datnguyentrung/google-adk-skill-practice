"""
Identity policy dành riêng cho Product Sales Knowledge Graph.

Lưu ý:
- Không sửa ontology.json.
- Đây là operational policy của ingestion skill.
- Chỉ khai báo natural key khi có identifier/code đủ rõ.
"""

PRODUCT_SALES_NATURAL_KEYS: dict[str, str] = {
    "pskg:BankingProduct": "pskg:productCode",
    "pskg:ProductCategory": "pskg:categoryCode",
    "pskg:CustomerSegment": "pskg:segmentCode",
    "pskg:CustomerNeed": "pskg:needCode",
    "pskg:ProductBundle": "pskg:bundleCode",
    "pskg:ApprovalTask": "pskg:approvalTaskId",
    "pskg:Customer": "pskg:customerIdentifier",
}
