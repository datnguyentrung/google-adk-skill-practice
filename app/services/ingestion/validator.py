from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.services.ingestion.registry import OntologyRegistry


class OntologyValidator:
    """
    Validate GraphPatch theo ontology.

    Chịu trách nhiệm:
    - Kiểm tra class/node có tồn tại trong ontology.
    - Kiểm tra property thuộc đúng class và đúng kiểu dữ liệu.
    - Kiểm tra cardinality và ràng buộc giá trị cố định của property.
    - Kiểm tra edge đúng domain/range và cardinality quan hệ.
    - Kiểm tra một số semantic constraint quan trọng.
    """

    # Một số edge trỏ tới BusinessRule cần target node có pskg:ruleType tương ứng.
    RULE_TYPE_BY_EDGE = {
        "pskg:hasEligibilityRule": "ELIGIBILITY",
        "pskg:hasSalesConditionRule": "SALES_CONDITION",
        "pskg:governedByPolicy": "POLICY",
    }

    def __init__(self, registry: OntologyRegistry):
        # Registry cung cấp API tra cứu class, attribute và edge đã được load từ ontology.
        self.registry = registry

    # =========================================================
    # NODE
    # =========================================================

    def validate_node(
        self,
        class_name: str,
        properties: dict[str, Any],
    ) -> list[str]:
        # Gom tất cả lỗi của node để caller sửa được nhiều vấn đề trong một lần.
        errors: list[str] = []

        # Tìm class trong ontology trước; class không tồn tại thì không thể validate tiếp.
        ontology_class = self.registry.get_class(class_name)

        if ontology_class is None:
            return [f"Unknown ontology class: {class_name}"]

        # 1. Kiểm tra từng property mà input truyền vào node.
        for property_name, value in properties.items():
            # Attribute chứa domain/range của property trong ontology.
            attribute = self.registry.get_attribute(property_name)

            if attribute is None:
                errors.append(f"Unknown ontology property: {property_name}")
                continue

            # Domain cho biết property được phép xuất hiện trên class nào.
            if ontology_class.name not in attribute.domain:
                errors.append(
                    f"Property {property_name} does not belong to class {class_name}"
                )
                continue

            # Range cho biết value phải thuộc datatype nào.
            if not self._is_valid_property_value(
                value,
                attribute.range,
            ):
                errors.append(
                    f"Invalid datatype for {property_name}: "
                    f"expected {attribute.range}, "
                    f"got {type(value).__name__}"
                )

        # 2. Kiểm tra thêm rule/cardinality được khai báo trên class.
        errors.extend(
            self._validate_attribute_rules(
                ontology_class=ontology_class,
                properties=properties,
            )
        )

        return list(dict.fromkeys(errors))

    def _validate_attribute_rules(
        self,
        ontology_class,
        properties: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []

        # Mỗi rule có dạng property + operator + value, ví dụ minQualified 1.
        for rule in ontology_class.rules:
            attribute = self.registry.get_attribute(rule.property)

            # Nếu rule.property không phải attribute thì có thể là edge rule.
            # Edge rule sẽ được xử lý riêng ở validate_graph_patch.
            if attribute is None:
                continue

            # Lấy value thực tế của property trong input node.
            value = properties.get(rule.property)

            # Scalar tính là 1, list tính theo số phần tử, None tính là 0.
            count = self._value_count(value)

            # -----------------------------------------
            # exactlyQualified
            # -----------------------------------------

            if rule.operator == "exactlyQualified":
                # Yêu cầu số lần xuất hiện phải bằng đúng expected.
                expected = self._to_int(rule.value)

                if expected is None:
                    continue

                if count == 0 and expected == 1:
                    errors.append(f"Missing required property: {rule.property}")
                elif count != expected:
                    errors.append(
                        f"Property {rule.property} must occur "
                        f"exactly {expected} time(s); got {count}"
                    )

            # -----------------------------------------
            # minQualified
            # -----------------------------------------

            elif rule.operator == "minQualified":
                # Yêu cầu số lần xuất hiện tối thiểu là minimum.
                minimum = self._to_int(rule.value)

                if minimum is None:
                    continue

                if count == 0 and minimum > 0:
                    errors.append(f"Missing required property: {rule.property}")
                elif count < minimum:
                    errors.append(
                        f"Property {rule.property} must occur "
                        f"at least {minimum} time(s); got {count}"
                    )

            # -----------------------------------------
            # some
            # -----------------------------------------

            elif rule.operator == "some":
                # Yêu cầu property tồn tại và thỏa datatype hoặc giá trị bắt buộc.
                errors.extend(
                    self._validate_some_attribute_rule(
                        rule=rule,
                        value=value,
                    )
                )

        return errors

    def _validate_some_attribute_rule(
        self,
        rule,
        value: Any,
    ) -> list[str]:
        """
        Ví dụ:

        status some Published
        → property phải tồn tại và = Published

        effectiveFrom some xsd:date
        → property phải tồn tại và đúng date.
        """

        if value is None:
            return [f"Missing required property: {rule.property}"]

        # rule.value là datatype hoặc giá trị bắt buộc đứng sau toán tử some.
        expected = rule.value

        if not isinstance(expected, str):
            return []

        # Dạng "some xsd:date/xsd:string/..." nghĩa là value phải đúng datatype.
        if expected.startswith("xsd:"):
            if not self._is_valid_property_value(
                value,
                [expected],
            ):
                return [f"Property {rule.property} must satisfy {expected}"]

            return []

        # Dạng "some Published/Running/..." nghĩa là value phải chứa giá trị này.
        values = value if isinstance(value, list) else [value]

        if expected not in values:
            return [f"Property {rule.property} must contain value {expected}"]

        return []

    # =========================================================
    # EDGE
    # =========================================================

    def validate_edge(
        self,
        edge_name: str,
        source_class_name: str,
        target_class_name: str,
        target_properties: dict[str, Any] | None = None,
    ) -> list[str]:
        errors: list[str] = []

        # Edge phải tồn tại trong ontology thì mới có domain/range để kiểm tra.
        edge = self.registry.get_edge(edge_name)

        if edge is None:
            return [f"Unknown ontology edge: {edge_name}"]

        # Lấy class nguồn và class đích để so với domain/range của edge.
        source_class = self.registry.get_class(source_class_name)

        target_class = self.registry.get_class(target_class_name)

        if source_class is None:
            errors.append(f"Unknown source class: {source_class_name}")

        if target_class is None:
            errors.append(f"Unknown target class: {target_class_name}")

        if errors:
            return errors

        # Domain: class nguồn có được phép đi ra edge này không.
        if source_class.name not in edge.domain:
            errors.append(
                f"Invalid edge domain for {edge_name}: "
                f"{source_class_name} is not allowed"
            )

        # Range: class đích có được phép nhận edge này không.
        if target_class.name not in edge.range:
            errors.append(
                f"Invalid edge range for {edge_name}: "
                f"{target_class_name} is not allowed"
            )

        # Một số edge có thêm ràng buộc nghiệp vụ ngoài domain/range.
        errors.extend(
            self._validate_edge_semantics(
                edge_name=edge_name,
                target_properties=target_properties or {},
            )
        )

        return errors

    def _validate_edge_semantics(
        self,
        edge_name: str,
        target_properties: dict[str, Any],
    ) -> list[str]:
        """
        Một số edge có constraint mạnh hơn domain/range.

        hasEligibilityRule
        → BusinessRule.ruleType = ELIGIBILITY

        hasSalesConditionRule
        → BusinessRule.ruleType = SALES_CONDITION

        governedByPolicy
        → BusinessRule.ruleType = POLICY
        """

        # Edge không nằm trong mapping thì không có constraint nghiệp vụ thêm.
        expected_rule_type = self.RULE_TYPE_BY_EDGE.get(edge_name)

        if expected_rule_type is None:
            return []

        # Hiện constraint nghiệp vụ chỉ kiểm tra pskg:ruleType của target node.
        actual_rule_type = target_properties.get("pskg:ruleType")

        if actual_rule_type != expected_rule_type:
            return [
                f"Edge {edge_name} requires target "
                f"pskg:ruleType={expected_rule_type}; "
                f"got {actual_rule_type}"
            ]

        return []

    # =========================================================
    # GRAPH
    # =========================================================

    def validate_graph_patch(
        self,
        patch,
    ) -> list[str]:
        errors: list[str] = []

        # Map temp_id -> node để edge có thể tham chiếu node trong cùng patch.
        node_by_temp_id = {node.temp_id: node for node in patch.nodes}

        # -----------------------------------------
        # 1. Validate tất cả node trước để bắt lỗi class/property/datatype.
        # -----------------------------------------

        for node in patch.nodes:
            node_errors = self.validate_node(
                class_name=node.class_name,
                properties=node.properties,
            )

            errors.extend(f"Node {node.temp_id}: {error}" for error in node_errors)

        # -----------------------------------------
        # 2. Validate edge: node nguồn/đích tồn tại và domain/range hợp lệ.
        # -----------------------------------------

        for edge in patch.edges:
            # Edge tham chiếu node bằng temp id trong patch.
            source = node_by_temp_id.get(edge.source_temp_id)

            target = node_by_temp_id.get(edge.target_temp_id)

            if source is None:
                errors.append(
                    f"Edge {edge.edge_name}: "
                    f"sourceTempId {edge.source_temp_id} "
                    f"does not exist"
                )
                continue

            if target is None:
                errors.append(
                    f"Edge {edge.edge_name}: "
                    f"targetTempId {edge.target_temp_id} "
                    f"does not exist"
                )
                continue

            edge_errors = self.validate_edge(
                edge_name=edge.edge_name,
                source_class_name=source.class_name,
                target_class_name=target.class_name,
                target_properties=target.properties,
            )

            errors.extend(f"Edge {edge.edge_name}: {error}" for error in edge_errors)

        # -----------------------------------------
        # 3. Validate cardinality quan hệ dựa trên edge rule của từng class.
        # -----------------------------------------

        errors.extend(
            self._validate_edge_cardinality(
                patch=patch,
                node_by_temp_id=node_by_temp_id,
            )
        )

        return errors

    def _validate_edge_cardinality(
        self,
        patch,
        node_by_temp_id,
    ) -> list[str]:
        errors: list[str] = []

        # Với mỗi node, đếm các outgoing edge rồi so với rule của class tương ứng.
        for node in patch.nodes:
            ontology_class = self.registry.get_class(node.class_name)

            if ontology_class is None:
                continue

            # Chỉ xét các edge đi ra từ node hiện tại.
            outgoing_edges = [
                edge for edge in patch.edges if edge.source_temp_id == node.temp_id
            ]

            for rule in ontology_class.rules:
                # Chỉ xử lý rule mà property là một edge trong ontology.
                ontology_edge = self.registry.get_edge(rule.property)

                # Không phải edge rule thì attribute validator đã xử lý hoặc bỏ qua.
                if ontology_edge is None:
                    continue

                # Đếm số edge cùng tên đi ra từ node hiện tại.
                count = sum(
                    1 for edge in outgoing_edges if edge.edge_name == rule.property
                )

                # exactlyQualified: số edge phải bằng đúng expected.
                if rule.operator == "exactlyQualified":
                    expected = self._to_int(rule.value)

                    if expected is not None and count != expected:
                        errors.append(
                            f"Node {node.temp_id}: "
                            f"edge {rule.property} must occur "
                            f"exactly {expected} time(s); "
                            f"got {count}"
                        )

                # minQualified: số edge phải đạt tối thiểu minimum.
                elif rule.operator == "minQualified":
                    minimum = self._to_int(rule.value)

                    if minimum is not None and count < minimum:
                        errors.append(
                            f"Node {node.temp_id}: "
                            f"edge {rule.property} must occur "
                            f"at least {minimum} time(s); "
                            f"got {count}"
                        )

                # some: cần ít nhất một edge theo rule.property.
                elif rule.operator == "some":
                    if count < 1:
                        errors.append(
                            f"Node {node.temp_id}: edge {rule.property} is required"
                        )

        return errors

    # =========================================================
    # DATATYPE HELPERS
    # =========================================================

    def _is_valid_property_value(
        self,
        value: Any,
        ranges: list[str],
    ) -> bool:

        # Property dạng list hợp lệ khi mọi phần tử đều khớp range.
        if isinstance(value, list):
            return all(
                self._is_valid_single_value(
                    item,
                    ranges,
                )
                for item in value
            )

        # Property scalar chỉ cần kiểm tra trực tiếp một value.
        return self._is_valid_single_value(
            value,
            ranges,
        )

    def _is_valid_single_value(
        self,
        value: Any,
        ranges: list[str],
    ) -> bool:
        # None hợp lệ ở tầng datatype; required/cardinality được kiểm tra ở rule.
        if value is None:
            return True

        # Một property có thể cho phép nhiều range; khớp một range là hợp lệ.
        for range_name in ranges:
            if range_name == "xsd:string":
                if isinstance(value, str):
                    return True

            elif range_name == "xsd:integer":
                # bool là subclass của int trong Python nên phải loại trừ rõ ràng.
                if isinstance(value, int) and not isinstance(value, bool):
                    return True

            elif range_name == "xsd:decimal":
                # Decimal chấp nhận int/float/Decimal, nhưng không chấp nhận bool.
                if isinstance(
                    value,
                    (int, float, Decimal),
                ) and not isinstance(value, bool):
                    return True

            elif range_name == "xsd:boolean":
                if isinstance(value, bool):
                    return True

            elif range_name == "xsd:date":
                if self._is_iso_date(value):
                    return True

            elif range_name == "xsd:dateTime":
                if self._is_iso_datetime(value):
                    return True

            elif range_name == "xsd:anyURI":
                # URI hiện chỉ yêu cầu là chuỗi không rỗng.
                if isinstance(value, str) and bool(value.strip()):
                    return True

        return False

    @staticmethod
    def _is_iso_date(value: Any) -> bool:
        # date object hợp lệ, nhưng datetime không được tính là date thuần.
        if isinstance(value, date) and not isinstance(
            value,
            datetime,
        ):
            return True

        if not isinstance(value, str):
            return False

        try:
            # Chuỗi phải parse được theo ISO date, ví dụ YYYY-MM-DD.
            date.fromisoformat(value)
            return True
        except ValueError:
            return False

    @staticmethod
    def _is_iso_datetime(value: Any) -> bool:
        # datetime object hợp lệ ngay lập tức.
        if isinstance(value, datetime):
            return True

        if not isinstance(value, str):
            return False

        try:
            # Hỗ trợ hậu tố Z bằng cách đổi sang offset +00:00 trước khi parse.
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False

    @staticmethod
    def _value_count(value: Any) -> int:
        # Không có value thì tính là không xuất hiện.
        if value is None:
            return 0

        # List biểu diễn property nhiều giá trị.
        if isinstance(value, list):
            return len(value)

        # Scalar có mặt thì tính là một lần xuất hiện.
        return 1

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            # Rule value từ ontology có thể là string nên convert an toàn sang int.
            return int(value)
        except (TypeError, ValueError):
            # Không convert được thì caller sẽ bỏ qua rule số này.
            return None
