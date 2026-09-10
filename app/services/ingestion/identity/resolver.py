"""Định danh node — quyết định khi nào hai node là cùng một thực thể.

Module này resolve identity theo thứ tự ưu tiên: định danh từ ontology → natural
key nghiệp vụ kèm source scope → hash nội dung làm phương án cuối. Kết quả được
dùng khi ghi Neo4j (MERGE theo identity) và khi kiểm tra trước persistence."""

import hashlib
import json
import logging
import re
import unicodedata
from typing import Any

from app.core.schemas.ingestion.identity import NodeIdentity
from app.services.ingestion.identity.policies import PRODUCT_SALES_NATURAL_KEYS
from app.services.ingestion.ontology.registry import OntologyRegistry

logger = logging.getLogger(__name__)


def source_scope_from_evidence(evidence) -> str | None:
    """
    Suy ra source scope (tên các nguồn tài liệu) từ danh sách evidence của node.
    """
    sources = sorted(
        {
            item.source.strip()
            for item in evidence
            if getattr(item, "source", None) and item.source.strip()
        }
    )
    return "|".join(sources) or None


class IdentityResolutionError(ValueError):
    """
    Lỗi khi không thể resolve identity cho một node.
    """



class IdentityResolver:
    """
    Resolve identity cho node theo ontology, natural key và source scope.
    """

    def __init__(
        self,
        registry: OntologyRegistry,
        natural_keys: dict[str, str],
    ):
        """
        Khởi tạo resolver và kiểm tra tính hợp lệ của policy natural key.

        Args:
            registry: Registry ontology để tra cứu class/property.
            natural_keys: Bản đồ class → property dùng làm natural key.

        Raises:
            IdentityResolutionError: Policy khai báo natural key không hợp lệ.
        """
        self.registry = registry
        self.natural_keys = natural_keys

        self._validate_policy()

    def resolve(
        self,
        class_name: str,
        properties: dict[str, Any],
        source_scope: str | None = None,
    ) -> NodeIdentity:
        """
        Resolve identity cho một node và trả về `NodeIdentity`.

        Args:
            class_name: Class ontology của node.
            properties: Giá trị thuộc tính của node.
            source_scope: Phạm vi nguồn tài liệu (nếu có).

        Returns:
            `NodeIdentity` với `strategy` là ontology/natural_key/hash/unresolved.

        Raises:
            IdentityResolutionError: Dữ liệu không đủ để tạo identity hợp lệ.
        """
        logger.info(
            "Resolving ingestion identity class_name=%s property_count=%s has_source_scope=%s",
            class_name,
            len(properties),
            source_scope is not None,
        )
        ontology_class = self.registry.get_class(class_name)

        if ontology_class is None:
            logger.warning(
                "Identity resolution failed unknown class_name=%s", class_name
            )
            raise IdentityResolutionError(f"Unknown ontology class: {class_name}")

        key_name = self.natural_keys.get(class_name)

        # Classes without a natural key use a deterministic source-scoped key.
        if key_name is None:
            if not source_scope:
                logger.warning(
                    "Identity resolution unresolved class_name=%s reason=no_natural_key_no_source_scope",
                    class_name,
                )
                return NodeIdentity(
                    class_name=class_name,
                    strategy="unresolved",
                    reason=(
                        "No natural identity policy and no source scope "
                        f"available for class {class_name}"
                    ),
                )

            logger.info(
                "Identity resolution using source-scoped strategy class_name=%s",
                class_name,
            )
            return self._source_scoped_identity(
                class_name=class_name,
                source_scope=source_scope,
                properties=properties,
            )

        value = properties.get(key_name)

        if value is None:
            if source_scope:
                return self._source_scoped_identity(
                    class_name=class_name,
                    source_scope=source_scope,
                    properties=properties,
                )
            logger.warning(
                "Identity resolution missing natural key class_name=%s key_name=%s",
                class_name,
                key_name,
            )
            raise IdentityResolutionError(
                f"Missing identity property {key_name} for class {class_name}"
            )

        normalized_value = self._normalize_value(value)

        if not normalized_value:
            if source_scope:
                return self._source_scoped_identity(
                    class_name=class_name,
                    source_scope=source_scope,
                    properties=properties,
                )
            logger.warning(
                "Identity resolution empty natural key class_name=%s key_name=%s",
                class_name,
                key_name,
            )
            raise IdentityResolutionError(
                f"Identity property {key_name} is empty for class {class_name}"
            )

        logger.info(
            "Identity resolution using natural key class_name=%s key_name=%s",
            class_name,
            key_name,
        )
        return NodeIdentity(
            class_name=class_name,
            strategy="natural_key",
            key_name=key_name,
            key_value=normalized_value,
        )

    def _source_scoped_identity(
        self,
        *,
        class_name: str,
        source_scope: str,
        properties: dict[str, Any],
    ) -> NodeIdentity:
        """
        Tạo identity theo natural key kết hợp source scope để tránh trùng giữa các tài liệu.
        """
        return NodeIdentity(
            class_name=class_name,
            strategy="source_scoped",
            key_name="_ingestionKey",
            key_value=self._build_source_scoped_key(
                class_name=class_name,
                source_scope=source_scope,
                properties=properties,
            ),
        )

    def _build_source_scoped_key(
        self,
        *,
        class_name: str,
        source_scope: str,
        properties: dict[str, Any],
    ) -> str:
        """
        Dựng chuỗi key thô (class + natural key + source scope) làm cơ sở băm identity.
        """
        payload = {
            "source": self._normalize_value(source_scope),
            "class": class_name,
            "properties": self._canonicalize_value(properties),
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _canonicalize_value(self, value: Any) -> Any:
        """
        Chuẩn hoá giá trị trước khi đưa vào key định danh (bỏ khoảng trắng, hạ chữ thường...).
        """
        if isinstance(value, dict):
            return {key: self._canonicalize_value(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [self._canonicalize_value(item) for item in value]
        if isinstance(value, str):
            return self._normalize_value(value)
        return value

    def _validate_policy(self) -> None:
        """
        Kiểm tra mọi natural key khai báo đều trỏ tới property thật trong ontology.
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
        """
        Chuẩn hoá một giá trị đơn thành chuỗi để so sánh/băm.
        """
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
    """
    Tạo resolver với policy natural key của Product Sales Knowledge Graph.
    """
    return IdentityResolver(
        registry=registry,
        natural_keys=PRODUCT_SALES_NATURAL_KEYS,
    )
