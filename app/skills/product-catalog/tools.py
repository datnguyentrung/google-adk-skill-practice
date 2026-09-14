"""Tool registration for the product-catalog skill."""

from app.tools.schema_tools import get_product_catalog_schema_tools


def get_tools() -> list:
    """Return schema-loader tools for the product-catalog skill."""
    return list(get_product_catalog_schema_tools())


__all__ = ["get_tools"]
