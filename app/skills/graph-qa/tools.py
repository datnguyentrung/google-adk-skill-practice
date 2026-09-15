"""Tool for graph-qa skill."""

from app.tools.graph_qa.cypher_query import (
    execute_read_cypher,
    hybrid_search,
    vector_search,
)


def get_tools() -> list:
    return [execute_read_cypher, vector_search, hybrid_search]


__all__ = ["get_tools"]
