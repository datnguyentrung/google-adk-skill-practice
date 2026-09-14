"""Tool registration for the business-rules skill."""

from app.tools.schema_tools import get_business_rules_schema_tools


def get_tools() -> list:
    """Return schema-loader tools for the business-rules skill."""
    return list(get_business_rules_schema_tools())


__all__ = ["get_tools"]
