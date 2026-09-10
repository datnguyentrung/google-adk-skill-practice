"""Phase 4 — Kiểm định graph patch trước khi ghi Knowledge Graph.

Package này là "cổng kiểm định" của pipeline ingestion: nhận graph patch do LLM
trích xuất và trả lời hai câu hỏi tách biệt:

1. `valid_for_extraction` — fact có được nguồn tài liệu chống đỡ và hợp lệ với
   ontology không (`source_grounding.py`, `ontology_validator.py`)?
2. `valid_for_persistence` — patch đã đủ điều kiện ghi xuống Neo4j chưa (rule bắt
   buộc, identity, coverage)?

Cấu trúc bên trong:
- `issues.py`: tiện ích issue/cardinality dùng chung.
- `support.py`: helper payload/log dùng chung.
- `semantic_judge.py`: tầng phán xử ngữ nghĩa bằng LLM (interface nhỏ, có fallback).
- `source_grounding.py`: đối chiếu patch với chunk nguồn.
- `ontology_validator.py`: đối chiếu patch với ontology.
- `graph_validation.py`: facade `GraphValidation` — interface chính của package.

Caller bên ngoài chỉ nên import từ package này (`from app.services.ingestion.validation
import GraphValidation`), không import xuyên vào module bên trong.
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
from app.services.ingestion.validation.semantic_judge import (
    DEFAULT_SEMANTIC_GROUNDING_MODEL,
    GeminiSemanticGroundingJudge,
    PermissiveSemanticGroundingJudge,
    SemanticGroundingDecision,
    SemanticGroundingJudge,
    SemanticValueJudge,
    create_default_semantic_grounding_judge,
    create_default_semantic_value_judge,
)
from app.services.ingestion.validation.source_grounding import SourceGroundingValidator

__all__ = [
    "DEFAULT_SEMANTIC_GROUNDING_MODEL",
    "GeminiSemanticGroundingJudge",
    "GraphPatchAssessment",
    "GraphValidation",
    "InvalidGraphPatchFragmentError",
    "OntologyValidator",
    "PermissiveSemanticGroundingJudge",
    "SemanticGroundingDecision",
    "SemanticGroundingJudge",
    "SemanticValueJudge",
    "SourceGroundingValidator",
    "cardinality_failure",
    "create_default_semantic_grounding_judge",
    "create_default_semantic_value_judge",
    "deduplicate_issues",
    "is_source_required_rule",
]
