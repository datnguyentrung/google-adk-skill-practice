"""Phase kiểm định — cổng vào (facade) `GraphValidation`.

Module này là interface chính của package `validation` cho phần còn lại của hệ
thống: nhận một graph patch thô + chunk nguồn, chạy compiler, rồi chạy lần lượt
kiểm định nguồn (`source_grounding`) và kiểm định ontology (`ontology_validator`).

Kết quả là `GraphPatchAssessment` gồm:
- `result`: kết luận `valid_for_extraction` / `valid_for_persistence` + issues;
- `compiled_patch`: patch đã compile (None nếu không compile được);
- `fingerprint`: dấu vân tay dùng để đảm bảo patch không đổi giữa validate và fill.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.trace_logger import pprint, trace_pprint

from pydantic import ValidationError

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft, GraphPatchFragment
from app.core.schemas.ingestion.validation import (
    GraphPatchValidationResult,
    ValidationIssue,
)
from app.services.ingestion.identity.resolver import (
    IdentityResolutionError,
    create_product_sales_identity_resolver,
    source_scope_from_evidence,
)
from app.services.ingestion.ontology.loader import OntologyLoader
from app.services.ingestion.ontology.registry import OntologyRegistry
from app.services.ingestion.patch.compiler import (
    DEFAULT_ONTOLOGY_PATH,
    CompiledGraphPatch,
    GraphPatchCompiler,
)
from app.services.ingestion.validation.ontology_validator import OntologyValidator
from app.services.ingestion.validation.source_grounding import SourceGroundingValidator


class InvalidGraphPatchFragmentError(ValueError):
    """Lỗi nghiệp vụ khi fragment không đủ điều kiện để tiếp tục pipeline."""

    error_kind = "invalid_graph_patch_fragment"
    retryable = True

    def __init__(self, message: str, *, summary: dict[str, Any] | None = None):
        """Ghi nhận message và summary chẩn đoán (tuỳ chọn) cho caller/log."""

        super().__init__(message)
        self.summary = summary or {}


@dataclass(frozen=True)
class GraphPatchAssessment:
    """Kết quả đánh giá một graph patch tại cổng kiểm định."""

    result: GraphPatchValidationResult
    compiled_patch: CompiledGraphPatch | None
    fingerprint: str | None


class GraphValidation:
    """Cổng kiểm định: compile patch rồi kiểm tra grounding + ontology."""

    def __init__(
        self,
        ontology_path: str | Path = DEFAULT_ONTOLOGY_PATH,
        *,
        compiler_schema_version: str | None = None,
        semantic_grounding_judge: Any = None,
    ):
        """Nạp ontology và dựng sẵn compiler, validator, identity resolver.

        Args:
            ontology_path: Đường dẫn file ontology JSON.
            compiler_schema_version: Ghi đè schema version của compiler (tuỳ chọn).
            semantic_grounding_judge: Judge quan hệ; bỏ trống thì dùng mặc định
                (permissive, hoặc Gemini nếu được bật qua biến môi trường).
        """

        ontology_path = Path(ontology_path)
        ontology = OntologyLoader.load(ontology_path)
        registry = OntologyRegistry(ontology)
        self.registry = registry
        compiler_kwargs: dict[str, Any] = {"ontology_path": ontology_path}
        if compiler_schema_version is not None:
            compiler_kwargs["schema_version"] = compiler_schema_version
        self.compiler = GraphPatchCompiler(**compiler_kwargs)
        self.validator = OntologyValidator(registry)
        self.source_grounding = SourceGroundingValidator(
            registry,
            semantic_judge=semantic_grounding_judge,
        )
        self.identity_resolver = create_product_sales_identity_resolver(registry)

    def assess(
        self,
        graph_patch: GraphPatchDraft | dict[str, Any],
        artifact_content_digest: str | None,
        source_chunks: list[DocumentChunk] | list[dict[str, Any]] | None = None,
    ) -> GraphPatchAssessment:
        """Đánh giá một graph patch và trả kết luận cho hai cổng extraction/persistence.

        Args:
            graph_patch: Draft patch (hoặc dict) do LLM trả về.
            artifact_content_digest: Digest của artifact nguồn, dùng cho fingerprint.
            source_chunks: Chunk nguồn đã chuẩn bị; `None` nghĩa là chưa có ngữ
                cảnh nguồn nên không thể cấp quyền persist.

        Returns:
            `GraphPatchAssessment`. Patch lỗi schema/compile trả về sớm với
            `compiled_patch=None`; patch hợp lệ được kiểm tra grounding trước,
            chỉ khi sạch mới chạy tiếp kiểm tra readiness cho persistence.
        """

        try:
            draft = GraphPatchDraft.model_validate(graph_patch)
        except ValidationError as exc:
            issues = self._schema_issues(exc)
            print(f"\n[TRACE][VALIDATION] GraphPatchDraft schema validation FAILED:")
            for issue in issues:
                print(f"  - {issue.code} at {issue.location}: {issue.message}")
            return GraphPatchAssessment(
                result=GraphPatchValidationResult(
                    valid_for_extraction=False,
                    valid_for_persistence=False,
                    errors=issues,
                    readiness_issues=[],
                    warnings=[],
                    node_count=self._collection_size(graph_patch, "nodes"),
                    edge_count=self._collection_size(graph_patch, "edges"),
                ),
                compiled_patch=None,
                fingerprint=None,
            )

        chunks = self._source_chunks(source_chunks)
        compiler_result = self.compiler.compile(draft)
        warning_issues = [
            ValidationIssue(
                code="SOURCE_WARNING",
                message=warning,
                location=f"warnings.{index}",
            )
            for index, warning in enumerate(draft.warnings)
        ]
        if compiler_result.compiled_patch is None:
            print(f"\n[TRACE][VALIDATION] Compiler failed to produce compiled_patch.")
            return GraphPatchAssessment(
                result=GraphPatchValidationResult(
                    valid_for_extraction=False,
                    valid_for_persistence=False,
                    errors=list(compiler_result.errors),
                    readiness_issues=[],
                    warnings=warning_issues,
                    node_count=len(draft.nodes),
                    edge_count=len(draft.edges),
                ),
                compiled_patch=None,
                fingerprint=None,
            )

        patch = compiler_result.compiled_patch
        extraction_issues = self._derived_edge_issues(draft)
        if chunks is not None:
            extraction_issues.extend(self.source_grounding.validate(draft, chunks))
        extraction_issues.extend(self.validator.validate_extraction(patch))

        # Chỉ kiểm tra readiness khi extraction đã sạch: tránh báo lỗi chồng chéo
        # và tránh tốn công kiểm tra trên dữ liệu chắc chắn phải sửa.
        readiness_issues: list[ValidationIssue] = []
        if not extraction_issues:
            readiness_issues.extend(self.validator.validate_persistence(patch))
            readiness_issues.extend(self._identity_preflight(patch))
            if chunks is None:
                readiness_issues.append(
                    ValidationIssue(
                        code="SOURCE_CONTEXT_REQUIRED",
                        message=(
                            "Prepared source chunks are required before a graph "
                            "patch can be authorized for persistence"
                        ),
                        location="graphPatch",
                    )
                )

        valid_for_extraction = not extraction_issues
        valid_for_persistence = valid_for_extraction and not readiness_issues
        
        fingerprint = self.compiler.fingerprint(patch, artifact_content_digest)
        assessment_result = GraphPatchValidationResult(
            valid_for_extraction=valid_for_extraction,
            valid_for_persistence=valid_for_persistence,
            errors=extraction_issues,
            readiness_issues=readiness_issues,
            warnings=warning_issues,
            node_count=len(patch.nodes),
            edge_count=len(patch.edges),
        )

        print(f"\n[TRACE][VALIDATION] Assessment Result:")
        print(f"  valid_for_extraction: {valid_for_extraction}")
        print(f"  valid_for_persistence: {valid_for_persistence}")
        print(f"  Fingerprint: {fingerprint}")
        if extraction_issues:
            print(f"  Extraction errors ({len(extraction_issues)}):")
            for err in extraction_issues:
                print(f"    - {err.code} at {err.location}: {err.message}")
        if readiness_issues:
            print(f"  Readiness issues ({len(readiness_issues)}):")
            for r_err in readiness_issues:
                print(f"    - {r_err.code} at {r_err.location}: {r_err.message}")

        return GraphPatchAssessment(
            result=assessment_result,
            compiled_patch=patch,
            fingerprint=fingerprint,
        )

    def validate_fragment_terms(
        self,
        fragment: GraphPatchFragment,
    ) -> list[ValidationIssue]:
        """Validate ontology terms that are knowable within one batch fragment.

        Cross-batch endpoint references are intentionally allowed here; their
        existence is resolved by persistent staging.
        """
        issues: list[ValidationIssue] = []
        local_classes: dict[str, str] = {}

        for node_index, node in enumerate(fragment.nodes):
            ontology_class = self.registry.get_class(node.class_name)
            if ontology_class is None:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_CLASS",
                        message=f"Unknown ontology class: {node.class_name}",
                        location=f"nodes.{node_index}.className",
                        node_temp_id=node.temp_id,
                    )
                )
                continue
            local_classes[node.temp_id] = ontology_class.name
            for property_index, prop in enumerate(node.properties):
                attribute = self.registry.get_attribute(prop.property_name)
                if attribute is None:
                    issues.append(
                        ValidationIssue(
                            code="UNKNOWN_PROPERTY",
                            message=f"Unknown ontology property: {prop.property_name}",
                            location=f"nodes.{node_index}.properties.{property_index}.propertyName",
                            node_temp_id=node.temp_id,
                            property_name=prop.property_name,
                        )
                    )
                elif ontology_class.name not in attribute.domain:
                    issues.append(
                        ValidationIssue(
                            code="PROPERTY_DOMAIN_MISMATCH",
                            message=f"Property {prop.property_name} does not belong to class {node.class_name}",
                            location=f"nodes.{node_index}.properties.{property_index}.propertyName",
                            node_temp_id=node.temp_id,
                            property_name=prop.property_name,
                        )
                    )

        for edge_index, edge in enumerate(fragment.edges):
            ontology_edge = self.registry.get_edge(edge.edge_name)
            if ontology_edge is None:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_EDGE",
                        message=f"Unknown ontology edge: {edge.edge_name}",
                        location=f"edges.{edge_index}.edgeName",
                        edge_name=edge.edge_name,
                    )
                )
                continue
            source_class = local_classes.get(edge.source_temp_id)
            target_class = local_classes.get(edge.target_temp_id)
            if source_class is not None and source_class not in ontology_edge.domain:
                issues.append(
                    ValidationIssue(
                        code="EDGE_DOMAIN_MISMATCH",
                        message=f"Invalid edge domain for {edge.edge_name}",
                        location=f"edges.{edge_index}.sourceTempId",
                        edge_name=edge.edge_name,
                    )
                )
            if target_class is not None and target_class not in ontology_edge.range:
                issues.append(
                    ValidationIssue(
                        code="EDGE_RANGE_MISMATCH",
                        message=f"Invalid edge range for {edge.edge_name}",
                        location=f"edges.{edge_index}.targetTempId",
                        edge_name=edge.edge_name,
                    )
                )

        return issues

    @staticmethod
    def _source_chunks(
        source_chunks: list[DocumentChunk] | list[dict[str, Any]] | None,
    ) -> list[DocumentChunk] | None:
        """Chuẩn hoá chunk nguồn (dict → DocumentChunk); `None` giữ nguyên `None`."""

        if source_chunks is None:
            return None
        return [
            item
            if isinstance(item, DocumentChunk)
            else DocumentChunk.model_validate(item)
            for item in source_chunks
        ]

    def _derived_edge_issues(self, draft: GraphPatchDraft) -> list[ValidationIssue]:
        """Bắt lỗi BusinessRule thiếu quan hệ nguồn để suy diễn `pskg:ruleType`."""

        rule_type_property = "pskg:ruleType"
        deriving_edges = self.registry.edge_names_deriving_property(rule_type_property)
        if not deriving_edges:
            return []
        derived_targets = {
            edge.target_temp_id
            for edge in draft.edges
            if edge.edge_name in deriving_edges
        }
        return [
            ValidationIssue(
                code="DERIVED_PROPERTY_REQUIRES_EDGE_EVIDENCE",
                message=(
                    f"BusinessRule node {node.temp_id} requires an incoming "
                    f"relationship that derives {rule_type_property}"
                ),
                location=f"nodes.{index}.properties.{rule_type_property}",
                node_temp_id=node.temp_id,
                property_name=rule_type_property,
            )
            for index, node in enumerate(draft.nodes)
            if node.class_name == "pskg:BusinessRule"
            and node.temp_id not in derived_targets
        ]

    def _identity_preflight(
        self,
        patch: CompiledGraphPatch,
    ) -> list[ValidationIssue]:
        """Kiểm tra trước mọi node đều resolve được identity trước khi persist."""

        issues: list[ValidationIssue] = []
        for node_index, node in enumerate(patch.nodes):
            try:
                identity = self.identity_resolver.resolve(
                    class_name=node.class_name,
                    properties=node.properties,
                    source_scope=source_scope_from_evidence(node.evidence),
                )
            except IdentityResolutionError as exc:
                issues.append(
                    ValidationIssue(
                        code="IDENTITY_UNRESOLVED",
                        message=str(exc),
                        location=f"nodes.{node_index}",
                        node_temp_id=node.temp_id,
                    )
                )
                continue
            if identity.strategy == "unresolved":
                issues.append(
                    ValidationIssue(
                        code="IDENTITY_UNRESOLVED",
                        message=identity.reason or "Identity could not be resolved",
                        location=f"nodes.{node_index}",
                        node_temp_id=node.temp_id,
                    )
                )
        return issues

    @staticmethod
    def _schema_issues(exc: ValidationError) -> list[ValidationIssue]:
        """Quy đổi lỗi pydantic của draft thành issue có mã lỗi ổn định."""

        issues: list[ValidationIssue] = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"])
            if error["type"] == "string_pattern_mismatch":
                code = "TECHNICAL_NAME_INVALID"
            elif "evidence" in error["loc"]:
                code = "EVIDENCE_INVALID"
            else:
                code = "SCHEMA_INVALID"
            issues.append(
                ValidationIssue(
                    code=code,
                    message=error["msg"],
                    location=location or "graphPatch",
                )
            )
        return issues

    @staticmethod
    def _collection_size(value: Any, key: str) -> int:
        """Đếm số phần tử của một collection trong payload thô (0 nếu không hợp lệ)."""

        if isinstance(value, dict) and isinstance(value.get(key), list):
            return len(value[key])
        return 0
