from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.schemas.ingestion.document import DocumentChunk
from app.core.schemas.ingestion.graph_patch import GraphPatchDraft
from app.core.schemas.ingestion.validation import (
    GraphPatchValidationResult,
    ValidationIssue,
)
from app.services.ingestion.graph_patch_compiler import (
    DEFAULT_ONTOLOGY_PATH,
    CompiledGraphPatch,
    GraphPatchCompiler,
)
from app.services.ingestion.identity import (
    IdentityResolutionError,
    create_product_sales_identity_resolver,
    source_scope_from_evidence,
)
from app.services.ingestion.loader import OntologyLoader
from app.services.ingestion.registry import OntologyRegistry
from app.services.ingestion.semantic_grounding import SemanticGroundingJudge
from app.services.ingestion.source_grounding import SourceGroundingValidator
from app.services.ingestion.validator import OntologyValidator


@dataclass(frozen=True)
class GraphPatchAssessment:
    result: GraphPatchValidationResult
    compiled_patch: CompiledGraphPatch | None
    fingerprint: str | None


class GraphPatchValidationService:
    def __init__(
        self,
        ontology_path: str | Path = DEFAULT_ONTOLOGY_PATH,
        *,
        compiler_schema_version: str | None = None,
        semantic_grounding_judge: SemanticGroundingJudge | None = None,
    ):
        ontology_path = Path(ontology_path)
        ontology = OntologyLoader.load(ontology_path)
        registry = OntologyRegistry(ontology)
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
        try:
            draft = GraphPatchDraft.model_validate(graph_patch)
        except ValidationError as exc:
            issues = self._schema_issues(exc)
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

        chunks = (
            None
            if source_chunks is None
            else [
                item
                if isinstance(item, DocumentChunk)
                else DocumentChunk.model_validate(item)
                for item in source_chunks
            ]
        )

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
        extraction_issues: list[ValidationIssue] = []
        if chunks is not None:
            extraction_issues.extend(self.source_grounding.validate(draft, chunks))
        extraction_issues.extend(self.validator.validate_extraction(patch))
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
        return GraphPatchAssessment(
            result=GraphPatchValidationResult(
                valid_for_extraction=valid_for_extraction,
                valid_for_persistence=valid_for_persistence,
                errors=extraction_issues,
                readiness_issues=readiness_issues,
                warnings=warning_issues,
                node_count=len(patch.nodes),
                edge_count=len(patch.edges),
            ),
            compiled_patch=patch,
            fingerprint=fingerprint,
        )

    def fingerprint_candidate(
        self,
        graph_patch: GraphPatchDraft | dict[str, Any],
        artifact_content_digest: str | None,
    ) -> str | None:
        """Compile only enough state to enforce the invocation gate."""

        try:
            draft = GraphPatchDraft.model_validate(graph_patch)
        except ValidationError:
            return None
        compiler_result = self.compiler.compile(draft)
        if compiler_result.compiled_patch is None:
            return None
        return self.compiler.fingerprint(
            compiler_result.compiled_patch,
            artifact_content_digest,
        )

    def _identity_preflight(
        self,
        patch: CompiledGraphPatch,
    ) -> list[ValidationIssue]:
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
        if isinstance(value, dict) and isinstance(value.get(key), list):
            return len(value[key])
        return 0
