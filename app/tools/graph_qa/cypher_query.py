# Neo4j graph query tools for ADK Skill Framework.

import json
import math
import re
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path
from typing import Any

from neo4j import RoutingControl
from neo4j.time import Date, DateTime, Duration, Time

from app.config.neo4j import Neo4jClient
from app.services.ingestion.identity.semantic_resolution import GoogleEmbeddingProvider
from app.services.ingestion.ontology.loader import OntologyLoader
from app.services.ingestion.ontology.registry import OntologyRegistry

FORBIDDEN_CYPHER_KEYWORDS = {
    "CREATE",
    "MERGE",
    "DELETE",
    "DETACH",
    "SET",
    "REMOVE",
    "DROP",
}
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Detects OWL namespace-prefixed property access in Cypher (e.g. p.pskg:propName).
# Valid Cypher uses p.propName; the pskg: prefix is ontology-only and causes
# a CypherSyntaxError that surfaces as "Invalid input 'CONTAINS'" or similar.
_NAMESPACE_PROPERTY_PATTERN = re.compile(r"\.\s*[A-Za-z_][A-Za-z0-9_]*\s*:[A-Za-z]")
DEFAULT_CANDIDATE_LIMIT = 50
DEFAULT_TOP_K = 5
ONTOLOGY_PATH = Path(
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)


def get_driver():
    """Get the Neo4j driver instance from Neo4jClient."""
    return Neo4jClient.get_driver()


def execute_read_cypher(
    cypher: str,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Execute a read-only Cypher query against the Neo4j knowledge graph.

    The caller must generate Cypher using only nodes, relationships,
    and properties defined by the loaded schema skill.
    """

    validate_read_only_cypher(cypher)

    driver = get_driver()
    database = Neo4jClient.database_name

    records, summary, keys = driver.execute_query(
        cypher,
        parameters_=parameters or {},
        database_=database,
        routing_=RoutingControl.READ,
    )

    return {
        "records": [_to_json_safe(record.data()) for record in records],
        "columns": list(keys),
        "count": len(records),
        "strategy": "cypher",
    }


def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(v) for v in value]

    if isinstance(value, (DateTime, Date, Time)):
        return value.iso_format()

    if isinstance(value, Duration):
        return str(value)

    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    return str(value)


def validate_read_only_cypher(cypher: str) -> None:
    if not isinstance(cypher, str) or not cypher.strip():
        raise ValueError("Cypher query must be a non-empty string.")

    if ";" in cypher:
        raise ValueError("Only a single read-only Cypher statement is allowed.")

    normalized = _strip_cypher_comments_and_strings(cypher).upper()

    for keyword in FORBIDDEN_CYPHER_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", normalized):
            raise ValueError(
                f"Only read-only Cypher is allowed. Forbidden keyword: {keyword}"
            )

    cleaned = _strip_cypher_comments_and_strings(cypher)
    normalized = cleaned.upper()

    for keyword in FORBIDDEN_CYPHER_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", normalized):
            raise ValueError(
                f"Only read-only Cypher is allowed. Forbidden keyword: {keyword}"
            )

        if _NAMESPACE_PROPERTY_PATTERN.search(cleaned):
            raise ValueError(
                "Invalid Cypher: property keys must not include a namespace prefix. "
                "Use the attribute's localName (e.g. 'p.bankingProductName') instead of "
                "the technicalName with prefix (e.g. 'p.pskg:bankingProductName')."
            )


def vector_search(
    query: str,
    schema: dict[str, Any] | None = None,
    labels: list[str] | None = None,
    top_k: int = DEFAULT_TOP_K,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> dict[str, Any]:
    """Find semantically similar graph nodes by ranking Neo4j candidates in-app.

    Use this for semantic discovery when the user describes needs, intent, or
    near-meaning content and the exact graph entity is not known yet.
    """

    query = _validate_query_text(query)
    top_k = _positive_int(top_k, "top_k")
    candidate_limit = _positive_int(candidate_limit, "candidate_limit")
    search_labels = _resolve_labels(schema=schema, labels=labels)

    candidates = _fetch_candidate_nodes(search_labels, candidate_limit)
    if not candidates:
        return {
            "records": [],
            "count": 0,
            "strategy": "vector",
            "labels": search_labels,
        }

    embedding_provider = _embedding_provider()
    query_vector = embedding_provider.embed(query)
    ranked: list[dict[str, Any]] = []

    for candidate in candidates:
        evidence_text = _candidate_text(candidate)
        if not evidence_text:
            continue
        score = _cosine(query_vector, embedding_provider.embed(evidence_text))
        ranked.append(
            {
                "node_id": candidate["node_id"],
                "labels": candidate["labels"],
                "properties": candidate["properties"],
                "score": score,
                "evidence_text": evidence_text,
            }
        )

    ranked.sort(key=lambda item: (-float(item["score"]), str(item["node_id"])))
    records = ranked[:top_k]
    return {
        "records": records,
        "count": len(records),
        "strategy": "vector",
        "labels": search_labels,
    }


def hybrid_search(
    query: str,
    expansion_cypher: str | None = None,
    schema: dict[str, Any] | None = None,
    labels: list[str] | None = None,
    top_k: int = DEFAULT_TOP_K,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run semantic discovery first, then expand/filter around candidate nodes."""

    semantic_result = vector_search(
        query=query,
        schema=schema,
        labels=labels,
        top_k=top_k,
        candidate_limit=candidate_limit,
    )
    candidate_ids = [record["node_id"] for record in semantic_result["records"]]
    if not candidate_ids:
        return {
            "semantic_hits": [],
            "graph_records": [],
            "count": 0,
            "strategy": "hybrid",
        }

    cypher = expansion_cypher or _default_expansion_cypher()
    validate_read_only_cypher(cypher)

    merged_parameters = dict(parameters or {})
    merged_parameters["candidate_ids"] = candidate_ids
    graph_result = execute_read_cypher(cypher, merged_parameters)

    return {
        "semantic_hits": semantic_result["records"],
        "graph_records": graph_result["records"],
        "columns": graph_result["columns"],
        "count": graph_result["count"],
        "strategy": "hybrid",
    }


def _strip_cypher_comments_and_strings(cypher: str) -> str:
    without_line_comments = re.sub(r"//.*?$", " ", cypher, flags=re.MULTILINE)
    without_block_comments = re.sub(
        r"/\*.*?\*/", " ", without_line_comments, flags=re.DOTALL
    )
    without_single_strings = re.sub(r"'(?:\\.|[^'\\])*'", " ", without_block_comments)
    return re.sub(r'"(?:\\.|[^"\\])*"', " ", without_single_strings)


def _validate_query_text(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string.")
    return query.strip()


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or int(value) < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return int(value)


def _validate_identifier(value: str, *, kind: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"Unsafe Neo4j {kind}: {value!r}")
    return value


def _resolve_labels(
    *,
    schema: dict[str, Any] | None,
    labels: list[str] | None,
) -> list[str]:
    resolved: list[str] = []
    for label in labels or []:
        resolved.append(_validate_identifier(label, kind="label"))

    for class_item in _schema_classes(schema):
        technical_name = class_item.get("technicalName") or class_item.get("name")
        if not technical_name:
            continue
        try:
            label = _label_for_class(str(technical_name))
        except Exception:
            continue
        resolved.append(_validate_identifier(label, kind="label"))

    return list(dict.fromkeys(resolved))


def _schema_classes(schema: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not schema:
        return []
    schema_payload = schema.get("schema", schema)
    classes = schema_payload.get("classes", [])
    return [item for item in classes if isinstance(item, dict)]


def _fetch_candidate_nodes(
    labels: list[str], candidate_limit: int
) -> list[dict[str, Any]]:
    label_filter = (
        "WHERE any(label IN labels(n) WHERE label IN $labels)" if labels else ""
    )
    cypher = f"""
    MATCH (n)
    {label_filter}
    RETURN elementId(n) AS node_id,
           labels(n) AS labels,
           properties(n) AS properties
    LIMIT $limit
    """
    result = execute_read_cypher(
        cypher,
        {
            "labels": labels,
            "limit": candidate_limit,
        },
    )
    return list(result["records"])


def _candidate_text(candidate: dict[str, Any]) -> str:
    payload = {
        "labels": candidate.get("labels") or [],
        "properties": {
            key: value
            for key, value in dict(candidate.get("properties") or {}).items()
            if value not in (None, "", [], {})
        },
    }
    if not payload["labels"] and not payload["properties"]:
        return ""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _default_expansion_cypher() -> str:
    return """
    MATCH (candidate)
    WHERE elementId(candidate) IN $candidate_ids
    OPTIONAL MATCH (candidate)-[outgoing]->(target)
    OPTIONAL MATCH (source)-[incoming]->(candidate)
    RETURN elementId(candidate) AS candidate_id,
           labels(candidate) AS candidate_labels,
           properties(candidate) AS candidate_properties,
           collect(DISTINCT {
               relationshipId: elementId(outgoing),
               type: type(outgoing),
               direction: 'outgoing',
               targetId: elementId(target),
               targetLabels: labels(target),
               targetProperties: properties(target)
           }) AS outgoing_relationships,
           collect(DISTINCT {
               relationshipId: elementId(incoming),
               type: type(incoming),
               direction: 'incoming',
               sourceId: elementId(source),
               sourceLabels: labels(source),
               sourceProperties: properties(source)
           }) AS incoming_relationships
    """


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _label_for_class(class_technical_name: str) -> str:
    ontology_class = _ontology_registry().get_class(class_technical_name)
    if ontology_class is None:
        raise ValueError(f"Unknown ontology class: {class_technical_name}")
    return _validate_identifier(ontology_class.local_name, kind="label")


@lru_cache(maxsize=1)
def _ontology_registry() -> OntologyRegistry:
    ontology = OntologyLoader.load(ONTOLOGY_PATH)
    return OntologyRegistry(ontology)


@lru_cache(maxsize=1)
def _embedding_provider() -> GoogleEmbeddingProvider:
    return GoogleEmbeddingProvider()


__all__ = [
    "execute_read_cypher",
    "hybrid_search",
    "validate_read_only_cypher",
    "vector_search",
]
