"""Schema Skill Registry — Mapping skill IDs to Ontology schema subsets."""

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.core.schemas.ingestion.models import OntologyDefinition
from app.services.ingestion.ontology.registry import OntologyRegistry
from app.tools.schema_tools import _load_ontology


# Mapping skill IDs to human-readable domain class names
SKILL_DOMAIN_CLASSES: dict[str, set[str]] = {
    "business-rules": {
        "business rule",
        "required document",
    },
    "product-catalog": {
        "banking product",
        "product offer",
        "product bundle",
    },
    "governance-versioning": {
        "version record",
        "approval task",
    },
    "campaign-targeting": {
        "campaign",
        "customer segment",
        "customer need",
    },
    "customer-recommendation": {
        "customer",
        "customer segment",
        "customer need",
    },
    "sales-enablement": {
        "sales script",
        "sales knowledge",
        "sales skill",
    },
}

SKILL_DESCRIPTIONS: dict[str, str] = {
    "business-rules": "Eligibility criteria, policy rules, sales conditions, qualification requirements, required documents, document types, document validity, rule priorities.",
    "product-catalog": "Banking products, product offers, product bundles, product codes, prices, fees, benefits, categories, cross-sell, upsell, substitution, complement, exclusion.",
    "governance-versioning": "Version records, approval tasks, version numbers, creation time, change descriptions, published versions, approval chains, approvers, publication lifecycle.",
    "campaign-targeting": "Campaigns, customer segments, customer needs, campaign names, objectives, status, budgets, validity periods, campaign-specific benefits or rules.",
    "customer-recommendation": "Customers, customer segments, customer needs, customer identifiers, behavior context, products used, recommended products, matching context.",
    "sales-enablement": "Sales scripts, sales knowledge, sales skills, sales scenarios, opening lines, objection handling, closing lines, sales guidance, FAQs, playbooks.",
}


@dataclass(frozen=True)
class SchemaSkillBundle:
    """Ontology schema subset cho một skill."""

    skill_id: str
    class_technical_names: list[str] = field(default_factory=list)
    attr_technical_names: list[str] = field(default_factory=list)
    edge_technical_names: list[str] = field(default_factory=list)
    description: str = ""


class SchemaSkillRegistry:
    """Quản lý tra cứu và load SchemaSkillBundle cho từng schema skill ID."""

    def __init__(self, ontology_registry: OntologyRegistry | None = None):
        if ontology_registry is None:
            ontology_def = _load_ontology()
            ontology_registry = OntologyRegistry(ontology_def)
        self.ontology_registry = ontology_registry
        self._bundles: dict[str, SchemaSkillBundle] = {}
        self._build_bundles()

    def _build_bundles(self) -> None:
        """Tạo SchemaSkillBundle cho từng skill từ OntologyRegistry."""
        ontology = self.ontology_registry.ontology

        for skill_id, domain_class_names in SKILL_DOMAIN_CLASSES.items():
            matched_classes = [
                cls for cls in ontology.classes if cls.name in domain_class_names
            ]
            class_tech_names = [cls.technical_name for cls in matched_classes]
            matched_class_names = {cls.name for cls in matched_classes}

            matched_attrs = [
                attr
                for attr in ontology.attributes
                if set(attr.domain) & matched_class_names
            ]
            attr_tech_names = [attr.technical_name for attr in matched_attrs]

            matched_edges = [
                edge
                for edge in ontology.edges
                if set(edge.domain) & matched_class_names
            ]
            edge_tech_names = [edge.technical_name for edge in matched_edges]

            description = SKILL_DESCRIPTIONS.get(skill_id, "")

            self._bundles[skill_id] = SchemaSkillBundle(
                skill_id=skill_id,
                class_technical_names=class_tech_names,
                attr_technical_names=attr_tech_names,
                edge_technical_names=edge_tech_names,
                description=description,
            )

    def get_bundle(self, skill_id: str) -> SchemaSkillBundle | None:
        """Lấy bundle của một skill theo skill_id."""
        return self._bundles.get(skill_id)

    def load(self, skill_ids: list[str]) -> list[SchemaSkillBundle]:
        """Load danh sách SchemaSkillBundle theo danh sách skill_ids."""
        return [
            self._bundles[s_id]
            for s_id in skill_ids
            if s_id in self._bundles
        ]

    def list_skills(self) -> list[str]:
        """Danh sách tất cả skill_id có sẵn trong registry."""
        return list(self._bundles.keys())

    def skill_description(self, skill_id: str) -> str:
        """Mô tả của skill."""
        bundle = self._bundles.get(skill_id)
        return bundle.description if bundle else ""


@lru_cache(maxsize=1)
def get_schema_skill_registry() -> SchemaSkillRegistry:
    """Singleton getter cho SchemaSkillRegistry."""
    return SchemaSkillRegistry()
