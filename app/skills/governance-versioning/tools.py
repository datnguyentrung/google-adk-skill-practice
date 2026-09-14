"""Tool registration for the governance-versioning skill."""

from app.tools.schema_tools import get_governance_versioning_schema_tools


def get_tools() -> list:
    """Return schema-loader tools for the governance-versioning skill."""
    return list(get_governance_versioning_schema_tools())


__all__ = ["get_tools"]
