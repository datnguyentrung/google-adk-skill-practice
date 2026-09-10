"""Định danh node — xác định khi nào hai node là cùng một thực thể.

`IdentityResolver` kết hợp định danh từ ontology, natural key nghiệp vụ (khai báo
trong `policies`) và source scope để tạo identity ổn định cho node trước khi ghi.
"""

from app.services.ingestion.identity.policies import PRODUCT_SALES_NATURAL_KEYS
from app.services.ingestion.identity.resolver import (
    IdentityResolutionError,
    IdentityResolver,
    create_product_sales_identity_resolver,
    source_scope_from_evidence,
)

__all__ = [
    "PRODUCT_SALES_NATURAL_KEYS",
    "IdentityResolutionError",
    "IdentityResolver",
    "create_product_sales_identity_resolver",
    "source_scope_from_evidence",
]
