"""Optional semantic/LLM fallback after deterministic natural-key resolution."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any, Protocol

from google import genai
from google.genai import types
from neo4j import Transaction

from app.services.ingestion.mapping.model_call import AdkStructuredCallExecutor


@dataclass(frozen=True)
class SemanticCandidate:
    node_id: str
    properties: dict[str, Any]
    score: float


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float]: ...


class MergeVerifier(Protocol):
    def verify(self, *, incoming: str, candidate: str, score: float) -> bool: ...


class GoogleEmbeddingProvider:
    def __init__(self, *, model: str | None = None, dimensions: int = 768) -> None:
        self.model = model or os.getenv("INGESTION_EMBEDDING_MODEL", "gemini-embedding-001")
        self.dimensions = dimensions
        self.client = genai.Client()

    def embed(self, text: str) -> list[float]:
        response = self.client.models.embed_content(
            model=self.model,
            contents=text,
            config=types.EmbedContentConfig(
                task_type="SEMANTIC_SIMILARITY",
                output_dimensionality=self.dimensions,
            ),
        )
        if not response.embeddings:
            raise RuntimeError("Embedding provider returned no vector")
        values = response.embeddings[0].values
        if values is None:
            raise RuntimeError("Embedding provider returned empty values")
        return [float(value) for value in values]


class AdkMergeVerifier:
    def __init__(self, *, model: str | None = None) -> None:
        resolved_model = model or os.getenv(
            "INGESTION_MODEL", os.getenv("GOOGLE_ADK_MODEL", "gemini-3.5-flash-lite")
        )
        self.executor = AdkStructuredCallExecutor(model=resolved_model)

    def verify(self, *, incoming: str, candidate: str, score: float) -> bool:
        payload = self.executor.run(
            operation="semantic_entity_resolution",
            instruction=(
                "Decide whether two same-class business knowledge graph nodes represent the "
                "same real entity. Be conservative: ambiguous means DISTINCT. Natural-key "
                "matching has already failed or was unavailable. Return MERGE only when the "
                "descriptions are clearly the same entity, not merely related or similar."
            ),
            output_schema={
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "enum": ["MERGE", "DISTINCT"]},
                    "reason": {"type": "string"},
                },
                "required": ["decision", "reason"],
                "additionalProperties": False,
            },
            message=json.dumps(
                {"incoming": incoming, "candidate": candidate, "similarity": score},
                ensure_ascii=False,
            ),
        )
        return payload.get("decision") == "MERGE"


class SemanticEntityResolver:
    """Search same-label candidates, rank by embedding, then conservatively verify."""

    def __init__(
        self,
        *,
        embedding_provider: EmbeddingProvider,
        verifier: MergeVerifier | None,
        soft_threshold: float = 0.80,
        hard_threshold: float = 0.95,
        candidate_limit: int = 50,
        allow_hard_merge: bool = False,
    ) -> None:
        if hard_threshold <= soft_threshold:
            raise ValueError(
                f"hard_threshold ({hard_threshold}) must be > soft_threshold ({soft_threshold})"
            )
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be >= 1")
        self.embedding_provider = embedding_provider
        self.verifier = verifier
        self.soft_threshold = soft_threshold
        self.hard_threshold = hard_threshold
        self.candidate_limit = candidate_limit
        self.allow_hard_merge = allow_hard_merge

    def resolve(
        self,
        tx: Transaction,
        *,
        class_name: str,
        properties: dict[str, Any],
        source_scope: str | None,
        mapper,
    ) -> SemanticCandidate | None:
        incoming_text = _semantic_text(properties, source_scope)
        if not incoming_text:
            return None
        label = mapper.class_to_label(class_name)
        records = tx.run(
            f"""
            MATCH (n:`{label}`)
            RETURN elementId(n) AS node_id, properties(n) AS properties
            LIMIT $limit
            """,
            limit=self.candidate_limit,
        )
        candidates = [dict(record) for record in records]
        if not candidates:
            return None

        incoming_vector = self.embedding_provider.embed(incoming_text)
        ranked: list[SemanticCandidate] = []
        for item in candidates:
            candidate_properties = dict(item.get("properties") or {})
            candidate_text = _semantic_text(candidate_properties, None)
            if not candidate_text:
                continue
            score = _cosine(incoming_vector, self.embedding_provider.embed(candidate_text))
            if score >= self.soft_threshold:
                ranked.append(
                    SemanticCandidate(
                        node_id=str(item["node_id"]),
                        properties=candidate_properties,
                        score=score,
                    )
                )
        if not ranked:
            return None
        ranked.sort(key=lambda item: item.score, reverse=True)
        best = ranked[0]

        if best.score >= self.hard_threshold and self.allow_hard_merge:
            return best
        if self.verifier is None:
            return None
        candidate_text = _semantic_text(best.properties, None)
        if self.verifier.verify(
            incoming=incoming_text,
            candidate=candidate_text,
            score=best.score,
        ):
            return best
        return None


def create_semantic_entity_resolver(*, model: str | None = None) -> SemanticEntityResolver | None:
    enabled = os.getenv("INGESTION_SEMANTIC_RESOLUTION_ENABLED", "false").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    return SemanticEntityResolver(
        embedding_provider=GoogleEmbeddingProvider(),
        verifier=AdkMergeVerifier(model=model),
        soft_threshold=float(os.getenv("INGESTION_SEMANTIC_SOFT_THRESHOLD", "0.80")),
        hard_threshold=float(os.getenv("INGESTION_SEMANTIC_HARD_THRESHOLD", "0.95")),
        candidate_limit=max(1, int(os.getenv("INGESTION_SEMANTIC_CANDIDATE_LIMIT", "50"))),
        allow_hard_merge=os.getenv(
            "INGESTION_SEMANTIC_ALLOW_HARD_MERGE", "false"
        ).strip().lower() in {"1", "true", "yes", "on"},
    )


def _semantic_text(properties: dict[str, Any], source_scope: str | None) -> str:
    filtered = {
        key: value
        for key, value in properties.items()
        if value not in (None, "", [], {}) and not str(key).startswith("_ingestion")
    }
    if source_scope:
        filtered["sourceScope"] = source_scope
    if not filtered:
        return ""
    return json.dumps(filtered, ensure_ascii=False, sort_keys=True, default=str)


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


__all__ = [
    "AdkMergeVerifier",
    "EmbeddingProvider",
    "GoogleEmbeddingProvider",
    "MergeVerifier",
    "SemanticCandidate",
    "SemanticEntityResolver",
    "create_semantic_entity_resolver",
]
