"""Tool registration for the campaign-targeting skill."""

from app.tools.schema_tools import get_campaign_targeting_schema_tools


def get_tools() -> list:
    """Return schema-loader tools for the campaign-targeting skill."""
    return list(get_campaign_targeting_schema_tools())


__all__ = ["get_tools"]
