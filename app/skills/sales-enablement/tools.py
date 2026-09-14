"""Tool registration for the sales-enablement skill."""

from app.tools.schema_tools import get_sales_enablement_schema_tools


def get_tools() -> list:
    """Return schema-loader tools for the business-rules skill."""
    return list(get_sales_enablement_schema_tools())


__all__ = ["get_tools"]
