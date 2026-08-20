import re
import unicodedata
from typing import Any

from app.core.schemas.ingestion.identity import NodeIdentity
from app.services.ingestion.policies.product_sales_identity import (
    PRODUCT_SALES_NATURAL_KEYS,
)
from app.services.ingestion.registry import OntologyRegistry


class IdentityResolutionError(ValueError):
    pass


class IdentityResolver:
    def __init__(
        self,
        registry: OntologyRegistry,
        natural_keys: dict[str, str],
    ):
        self.registry = registry
        self.natural_keys = natural_keys

        self._validate_policy()

    def resolve(
        self,
        class_name: str,
        properties: dict[str, Any],
    ) -> NodeIdentity:
        ontology_class = self.registry.get_class(class_name)

        if ontology_class is None:
            raise IdentityResolutionError(f"Unknown ontology class: {class_name}")

        key_name = self.natural_keys.get(class_name)

        # Class không có identity policy rõ ràng
        if key_name is None:
            return NodeIdentity(
                class_name=class_name,
                strategy="unresolved",
                reason=(f"No natural identity policy defined for class {class_name}"),
            )

        value = properties.get(key_name)

        if value is None:
            raise IdentityResolutionError(
                f"Missing identity property {key_name} for class {class_name}"
            )

        normalized_value = self._normalize_value(value)

        if not normalized_value:
            raise IdentityResolutionError(
                f"Identity property {key_name} is empty for class {class_name}"
            )

        return NodeIdentity(
            class_name=class_name,
            strategy="natural_key",
            key_name=key_name,
            key_value=normalized_value,
        )

    def _validate_policy(self) -> None:
        """
        Fail fast nếu mapping hard-code không còn khớp
        ontology đang load.

        Quan trọng:
        ontology vẫn là source of truth về schema.
        """

        for class_name, property_name in self.natural_keys.items():
            ontology_class = self.registry.get_class(class_name)

            if ontology_class is None:
                raise IdentityResolutionError(
                    f"Identity policy references unknown class: {class_name}"
                )

            attribute = self.registry.get_attribute(property_name)

            if attribute is None:
                raise IdentityResolutionError(
                    f"Identity policy references unknown property: {property_name}"
                )

            if ontology_class.name not in attribute.domain:
                raise IdentityResolutionError(
                    f"Identity property {property_name} "
                    f"does not belong to class {class_name}"
                )

    @staticmethod
    def _normalize_value(value: Any) -> str:
        text = str(value)

        text = unicodedata.normalize(
            "NFKC",
            text,
        )

        text = text.strip()

        text = re.sub(
            r"\s+",
            " ",
            text,
        )

        return text


def create_product_sales_identity_resolver(
    registry: OntologyRegistry,
) -> IdentityResolver:
    return IdentityResolver(
        registry=registry,
        natural_keys=PRODUCT_SALES_NATURAL_KEYS,
    )
