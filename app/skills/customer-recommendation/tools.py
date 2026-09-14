"""Tool registration for the customer-recommendation skill."""

from app.tools.schema_tools import get_customer_recommendation_schema_tools


def get_tools() -> list:
    """Return schema-loader tools for the customer-recommendation skill."""
    return list(get_customer_recommendation_schema_tools())


__all__ = ["get_tools"]
