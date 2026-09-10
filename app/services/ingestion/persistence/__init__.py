"""Phase 5 — Ghi graph patch xuống Neo4j và đọc lại để xác minh.

`GraphPersistence` là cổng ghi duy nhất: validate lần cuối, resolve identity, ghi
node/edge (`Neo4jGraphStore`), rồi đọc lại và đối chiếu (`verify_persisted_graph`).
"""

from app.services.ingestion.persistence.mapping import (
    Neo4jMapper,
    Neo4jMappingError,
)
from app.services.ingestion.persistence.readback import (
    relationship_key,
    verify_persisted_graph,
)
from app.services.ingestion.persistence.service import (
    FillValidationError,
    GraphPersistence,
    create_graph_persistence,
)
from app.services.ingestion.persistence.writer import (
    Neo4jGraphStore,
    Neo4jWriteError,
)

__all__ = [
    "FillValidationError",
    "GraphPersistence",
    "Neo4jGraphStore",
    "Neo4jMapper",
    "Neo4jMappingError",
    "Neo4jWriteError",
    "create_graph_persistence",
    "relationship_key",
    "verify_persisted_graph",
]
