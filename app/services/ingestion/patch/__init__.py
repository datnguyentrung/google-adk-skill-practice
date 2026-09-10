"""Phase 3 — Biên dịch và chốt chặn graph patch.

`GraphPatchCompiler` biến draft của LLM thành patch đã chuẩn hoá kèm fingerprint;
`GraphFragmentGuard` chạy các kiểm tra tất định cho từng fragment trước khi gộp.
"""

from app.services.ingestion.patch.compiler import (
    COMPILER_SCHEMA_VERSION,
    DEFAULT_ONTOLOGY_PATH,
    NO_ARTIFACT_DIGEST,
    CompiledEdge,
    CompiledGraphPatch,
    CompiledNode,
    CompilerResult,
    GraphPatchCompiler,
)
from app.services.ingestion.patch.fragment_guard import GraphFragmentGuard

__all__ = [
    "COMPILER_SCHEMA_VERSION",
    "CompiledEdge",
    "CompiledGraphPatch",
    "CompiledNode",
    "CompilerResult",
    "DEFAULT_ONTOLOGY_PATH",
    "GraphFragmentGuard",
    "GraphPatchCompiler",
    "NO_ARTIFACT_DIGEST",
]
