from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from app.scripts.preprocessing.cleaner import (
    CleanerConfig,
    clean_markdown,
)
from app.scripts.preprocessing.deduplicator import (
    DocumentDedupIndex,
    document_fingerprint,
    exact_line_dedup,
    normalized_line_dedup,
    section_dedup,
    semantic_fact_dedup,
)
from app.scripts.preprocessing.normalizer import (
    normalize_markdown,
)
from app.scripts.preprocessing.validator import (
    FieldRule,
    ValidationResult,
    ValidationSeverity,
    validate_markdown,
)


@dataclass(frozen=True)
class MarkdownPreprocessorConfig:
    attribute_aliases: Mapping[str, str] = field(default_factory=dict)

    field_rules: Mapping[str, FieldRule] = field(default_factory=dict)

    cleaner: CleanerConfig = field(default_factory=CleanerConfig)

    rewrite_values: bool = True

    conflict_severity: ValidationSeverity = ValidationSeverity.WARNING

    reject_on_validation_error: bool = False

    duplicate_document_severity: ValidationSeverity = ValidationSeverity.WARNING


@dataclass(frozen=True)
class MarkdownPreprocessResult:
    raw_text: str

    pre_deduplicated_text: str

    cleaned_text: str

    normalized_text: str

    processed_text: str

    validation: ValidationResult

    fingerprint: str

    duplicate_document: bool = False

    @property
    def is_valid(self) -> bool:

        return self.validation.is_valid


class MarkdownPreprocessingError(ValueError):
    def __init__(
        self,
        result: MarkdownPreprocessResult,
    ) -> None:

        self.result = result

        messages = "; ".join(issue.message for issue in result.validation.errors)

        super().__init__(messages or ("Markdown preprocessing validation failed."))


class MarkdownPreprocessor:
    def __init__(
        self,
        config: MarkdownPreprocessorConfig | None = None,
        *,
        document_dedup_index: (DocumentDedupIndex | None) = None,
    ) -> None:

        self.config = config or MarkdownPreprocessorConfig()

        self.document_dedup_index = document_dedup_index

    def preprocess(
        self,
        raw_text: str,
    ) -> MarkdownPreprocessResult:

        aliases = self.config.attribute_aliases

        # ==================================================
        # STEP 1
        # DEDUP RAW
        # ==================================================

        pre_deduplicated = exact_line_dedup(raw_text)

        # ==================================================
        # STEP 2
        # CLEAN
        # ==================================================

        cleaned = clean_markdown(
            pre_deduplicated,
            config=self.config.cleaner,
        )

        # ==================================================
        # STEP 3
        # NORMALIZE
        # ==================================================

        normalized = normalize_markdown(
            cleaned,
            attribute_aliases=aliases,
            rewrite_values=(self.config.rewrite_values),
        )

        # ==================================================
        # STEP 4
        # DEDUP AFTER NORMALIZATION
        # ==================================================

        processed = normalized_line_dedup(normalized)

        processed = semantic_fact_dedup(
            processed,
            attribute_aliases=aliases,
        )

        processed = section_dedup(processed)

        # Clean lại formatting sau transformations.
        processed = clean_markdown(
            processed,
            config=self.config.cleaner,
        )

        # ==================================================
        # STEP 5
        # VALIDATE
        # ==================================================

        validation = validate_markdown(
            processed,
            attribute_aliases=aliases,
            field_rules=(self.config.field_rules),
            conflict_severity=(self.config.conflict_severity),
        )

        # ==================================================
        # STEP 6
        # DOCUMENT DEDUP
        # ==================================================

        duplicate_document = False

        if self.document_dedup_index is not None:
            duplicate_document = self.document_dedup_index.check_and_add(processed)

            if duplicate_document:
                validation.add(
                    code="DUPLICATE_DOCUMENT",
                    message=(
                        "Document content already exists in the ingestion dedup index."
                    ),
                    severity=(self.config.duplicate_document_severity),
                )

        # ==================================================
        # RESULT
        # ==================================================

        result = MarkdownPreprocessResult(
            raw_text=raw_text,
            pre_deduplicated_text=(pre_deduplicated),
            cleaned_text=cleaned,
            normalized_text=normalized,
            processed_text=processed,
            validation=validation,
            fingerprint=(document_fingerprint(processed)),
            duplicate_document=(duplicate_document),
        )

        if self.config.reject_on_validation_error and not result.is_valid:
            raise MarkdownPreprocessingError(result)

        return result
