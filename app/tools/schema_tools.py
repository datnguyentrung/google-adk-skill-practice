"""Schema-loader tools for the Product Sales Knowledge Graph ingestion skills.

Each tool filters the full PSKG ontology down to the classes, attributes,
and edges that belong to its domain, and returns them as a structured dict.
The ingestion LLM uses this dict as the sole ontology contract when extracting
graph facts from a document batch.

Pattern followed by all loaders
--------------------------------
1. Load (and cache) the full OntologyDefinition via OntologyLoader.
2. Filter to the relevant class names (``_DOMAIN_CLASSES`` per domain).
3. Collect attributes whose ``domain`` intersects the class set.
4. Collect edges whose ``domain`` intersects the class set.
5. Return ``{"success": True, "schema": {...}}`` on success or
   ``{"success": False, "errorCode": ..., "errorMessage": ...}`` on failure.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.schemas.ingestion.models import OntologyDefinition
from app.services.ingestion.ontology.loader import OntologyLoader

# ---------------------------------------------------------------------------
# Ontology source
# ---------------------------------------------------------------------------

_ONTOLOGY_PATH = Path(
    "app/data/ontology/product_sales_knowledge_graph_base_v3_1.ontology.json"
)

# Domain → set of OWL class ``name`` values (human-readable, not technicalName)
# that belong to the domain.  Edges/attributes are included when their ``domain``
# list intersects this set.

_BUSINESS_RULES_CLASSES = {
    "business rule",
    "required document",
}

_PRODUCT_CATALOG_CLASSES = {
    "banking product",
    "product offer",
    "product bundle",
}

_GOVERNANCE_VERSIONING_CLASSES = {
    "version record",
    "approval task",
}

_CAMPAIGN_TARGETING_CLASSES = {
    "campaign",
    "customer segment",
    "customer need",
}

_CUSTOMER_RECOMMENDATION_CLASSES = {
    "customer",
    "customer segment",
    "customer need",
}

_SALES_ENABLEMENT_CLASSES = {
    "sales script",
    "sales knowledge",
    "sales skill",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_ontology() -> OntologyDefinition:
    """Load and cache the full PSKG ontology exactly once per process."""
    return OntologyLoader.load(_ONTOLOGY_PATH)


def _build_domain_schema(class_names: set[str]) -> dict[str, Any]:
    """Filter ontology thành một schema bundle khép kín theo domain.

    Bao gồm:
    - các class chính của skill;
    - edge có domain HOẶC range chạm vào class chính;
    - endpoint class của các edge đó (one-hop closure);
    - attribute của các class nằm trong closure.

    Chỉ closure một hop để tránh kéo cả ontology vào skill.
    """
    ontology = _load_ontology()

    # 1. Primary classes của skill.
    primary_classes = [cls for cls in ontology.classes if cls.name in class_names]
    primary_class_names = {cls.name for cls in primary_classes}

    # 2. Edge chạm vào primary classes ở DOMAIN hoặc RANGE.
    matched_edges = [
        edge
        for edge in ontology.edges
        if ((set(edge.domain) | set(edge.range)) & primary_class_names)
    ]

    # 3. One-hop closure:
    # nếu expose edge thì expose luôn endpoint classes.
    closure_class_names = set(primary_class_names)

    for edge in matched_edges:
        closure_class_names.update(edge.domain)
        closure_class_names.update(edge.range)

    matched_classes = [
        cls for cls in ontology.classes if cls.name in closure_class_names
    ]

    # 4. Attributes thuộc các classes trong closure.
    matched_attributes = [
        attr for attr in ontology.attributes if set(attr.domain) & closure_class_names
    ]

    def _serialize_class(cls):
        return {
            "name": cls.name,
            "technicalName": cls.technical_name,
            "label": cls.label,
            "definition": cls.definition,
            "parents": cls.parents,
            "rules": [
                {
                    "property": r.property,
                    "operator": r.operator,
                    "value": r.value,
                    "qualifier": r.qualifier,
                }
                for r in cls.rules
            ],
        }

    def _serialize_attribute(attr):
        return {
            "name": attr.name,
            "technicalName": attr.technical_name,
            "label": attr.label,
            "definition": attr.definition,
            "domain": attr.domain,
            "range": attr.range,
            "ingestionPolicy": {
                "mode": attr.ingestion_policy.mode,
                "defaultValue": attr.ingestion_policy.default_value,
                "grounding": attr.ingestion_policy.grounding,
                "deriveFromEdges": attr.ingestion_policy.derive_from_edges,
            },
        }

    def _serialize_edge(edge):
        return {
            "name": edge.name,
            "technicalName": edge.technical_name,
            "label": edge.label,
            "definition": edge.definition,
            "domain": edge.domain,
            "range": edge.range,
            "groundingCues": edge.grounding_cues,
        }

    return {
        "classes": [_serialize_class(c) for c in matched_classes],
        "attributes": [_serialize_attribute(a) for a in matched_attributes],
        "edges": [_serialize_edge(e) for e in matched_edges],
    }


def _ok(schema: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "schema": schema}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "errorCode": code, "errorMessage": message}


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------


def load_business_rules_schema() -> dict[str, Any]:
    """Load the PSKG ontology schema for the Business Rules domain.

    Returns classes, attributes, and edges for BusinessRule and
    RequiredDocument, covering eligibility criteria, policy conditions,
    sales conditions, qualification requirements, document types, and
    rule priorities.

    Use this tool before extracting business-rule or required-document facts
    from any ingestion batch.

    Returns:
        ``{"success": True, "schema": {"classes": [...], "attributes": [...],
        "edges": [...]}}`` or an error dict.
    """
    try:
        return _ok(_build_domain_schema(_BUSINESS_RULES_CLASSES))
    except FileNotFoundError as exc:
        return _error("ONTOLOGY_NOT_FOUND", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error("SCHEMA_LOAD_ERROR", str(exc))


def load_product_catalog_schema() -> dict[str, Any]:
    """Load the PSKG ontology schema for the Product Catalog domain.

    Returns classes, attributes, and edges for BankingProduct, ProductOffer,
    and ProductBundle, covering product codes, names, versions, statuses,
    prices, fees, benefits, categories, cross-sell, upsell, substitution,
    complement, and exclusion relationships.

    Use this tool before extracting product-catalog facts from any ingestion
    batch.

    Returns:
        ``{"success": True, "schema": {"classes": [...], "attributes": [...],
        "edges": [...]}}`` or an error dict.
    """
    try:
        return _ok(_build_domain_schema(_PRODUCT_CATALOG_CLASSES))
    except FileNotFoundError as exc:
        return _error("ONTOLOGY_NOT_FOUND", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error("SCHEMA_LOAD_ERROR", str(exc))


def load_governance_versioning_schema() -> dict[str, Any]:
    """Load the PSKG ontology schema for the Governance & Versioning domain.

    Returns classes, attributes, and edges for VersionRecord and ApprovalTask,
    covering version numbers, creation time, change descriptions, published
    versions, approval chains, approvers, steps, comments, approval history,
    and publication lifecycle information.

    Use this tool before extracting governance or versioning facts from any
    ingestion batch.

    Returns:
        ``{"success": True, "schema": {"classes": [...], "attributes": [...],
        "edges": [...]}}`` or an error dict.
    """
    try:
        return _ok(_build_domain_schema(_GOVERNANCE_VERSIONING_CLASSES))
    except FileNotFoundError as exc:
        return _error("ONTOLOGY_NOT_FOUND", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error("SCHEMA_LOAD_ERROR", str(exc))


def load_campaign_targeting_schema() -> dict[str, Any]:
    """Load the PSKG ontology schema for the Campaign Targeting domain.

    Returns classes, attributes, and edges for Campaign, CustomerSegment, and
    CustomerNeed, covering campaign names, objectives, status, budgets,
    validity periods, segments, needs, and campaign-specific benefits or rules.

    Use this tool before extracting campaign or targeting facts from any
    ingestion batch.

    Returns:
        ``{"success": True, "schema": {"classes": [...], "attributes": [...],
        "edges": [...]}}`` or an error dict.
    """
    try:
        return _ok(_build_domain_schema(_CAMPAIGN_TARGETING_CLASSES))
    except FileNotFoundError as exc:
        return _error("ONTOLOGY_NOT_FOUND", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error("SCHEMA_LOAD_ERROR", str(exc))


def load_customer_recommendation_schema() -> dict[str, Any]:
    """Load the PSKG ontology schema for the Customer Recommendation domain.

    Returns classes, attributes, and edges for Customer, CustomerSegment, and
    CustomerNeed, covering customer identifiers, behavior context, segments,
    needs, products currently used, recommended products, and product-matching
    context.

    Use this tool before extracting customer or recommendation facts from any
    ingestion batch.

    Returns:
        ``{"success": True, "schema": {"classes": [...], "attributes": [...],
        "edges": [...]}}`` or an error dict.
    """
    try:
        return _ok(_build_domain_schema(_CUSTOMER_RECOMMENDATION_CLASSES))
    except FileNotFoundError as exc:
        return _error("ONTOLOGY_NOT_FOUND", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error("SCHEMA_LOAD_ERROR", str(exc))


def load_sales_enablement_schema() -> dict[str, Any]:
    """Load the PSKG ontology schema for the Sales Enablement domain.

    Returns classes, attributes, and edges for SalesScript, SalesKnowledge,
    and SalesSkill, covering sales scenarios, opening lines, objection
    handling, closing lines, sales guidance, FAQs, articles, guides, and
    playbooks.

    Use this tool before extracting sales-enablement facts from any ingestion
    batch.

    Returns:
        ``{"success": True, "schema": {"classes": [...], "attributes": [...],
        "edges": [...]}}`` or an error dict.
    """
    try:
        return _ok(_build_domain_schema(_SALES_ENABLEMENT_CLASSES))
    except FileNotFoundError as exc:
        return _error("ONTOLOGY_NOT_FOUND", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error("SCHEMA_LOAD_ERROR", str(exc))


# ---------------------------------------------------------------------------
# Registry helpers (mirrors COOKING_TOOLS / INGESTION_TOOLS pattern)
# ---------------------------------------------------------------------------

SCHEMA_TOOLS = {
    "load_business_rules_schema": load_business_rules_schema,
    "load_product_catalog_schema": load_product_catalog_schema,
    "load_governance_versioning_schema": load_governance_versioning_schema,
    "load_campaign_targeting_schema": load_campaign_targeting_schema,
    "load_customer_recommendation_schema": load_customer_recommendation_schema,
    "load_sales_enablement_schema": load_sales_enablement_schema,
}


def get_schema_tools() -> list:
    """Return all schema-loader tool callables."""
    return list(SCHEMA_TOOLS.values())


def get_business_rules_schema_tools() -> list:
    return [load_business_rules_schema]


def get_product_catalog_schema_tools() -> list:
    return [load_product_catalog_schema]


def get_governance_versioning_schema_tools() -> list:
    return [load_governance_versioning_schema]


def get_campaign_targeting_schema_tools() -> list:
    return [load_campaign_targeting_schema]


def get_customer_recommendation_schema_tools() -> list:
    return [load_customer_recommendation_schema]


def get_sales_enablement_schema_tools() -> list:
    return [load_sales_enablement_schema]


__all__ = [
    "SCHEMA_TOOLS",
    "get_schema_tools",
    "get_business_rules_schema_tools",
    "get_product_catalog_schema_tools",
    "get_governance_versioning_schema_tools",
    "get_campaign_targeting_schema_tools",
    "get_customer_recommendation_schema_tools",
    "get_sales_enablement_schema_tools",
    "load_business_rules_schema",
    "load_campaign_targeting_schema",
    "load_customer_recommendation_schema",
    "load_governance_versioning_schema",
    "load_product_catalog_schema",
    "load_sales_enablement_schema",
]
