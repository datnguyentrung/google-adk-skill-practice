"""Phase 4 — Kiểm định graph patch trước khi ghi Knowledge Graph.

Package này là "cổng kiểm định" của pipeline ingestion: nhận graph patch do LLM
trích xuất và trả lời hai câu hỏi tách biệt:

1. `valid_for_extraction` — fact có được nguồn tài liệu chống đỡ và hợp lệ với
   ontology không (`source_grounding.py`, `ontology_validator.py`)?
2. `valid_for_persistence` — patch đã đủ điều kiện ghi xuống Neo4j chưa (rule bắt
   buộc, identity, coverage)?
"""

from app.services.ingestion.validation.graph_validation import (
    GraphPatchAssessment,
    GraphValidation,
    InvalidGraphPatchFragmentError,
)
from app.services.ingestion.validation.issues import (
    cardinality_failure,
    deduplicate_issues,
)
from app.services.ingestion.validation.ontology_validator import (
    OntologyValidator,
    is_source_required_rule,
)
from app.services.ingestion.validation.source_grounding import (
    SemanticGroundingDecision,
    SourceGroundingValidator,
)

__all__ = [
    "GraphPatchAssessment",
    "GraphValidation",
    "InvalidGraphPatchFragmentError",
    "OntologyValidator",
    "SemanticGroundingDecision",
    "SourceGroundingValidator",
    "cardinality_failure",
    "deduplicate_issues",
    "is_source_required_rule",
]
