"""Deterministic helpers phục vụ phiên ingestion (session state, context payload, receipts & stats)."""

from app.services.ingestion.orchestration.artifact import (
    load_and_prepare_artifact_context,
)
from app.services.ingestion.orchestration.context import _batch_payload
from app.services.ingestion.orchestration.receipts import (
    _persist_with_receipt,
    _public_assessment,
)
from app.services.ingestion.orchestration.state import (
    ARTIFACT_DIGEST_STATE_KEY,
    ARTIFACT_NAME_STATE_KEY,
    DOCUMENT_ID_STATE_KEY,
    INGESTION_SIGNATURE_STATE_KEY,
    SOURCE_CHUNKS_STATE_KEY,
    VALIDATED_FINGERPRINT_STATE_KEY,
    WORKSPACE_STATE_KEY,
    _clear_validation_gate,
    _current_provenance,
    _get_validation_service,
    _get_workspace_service,
    _load_workspace,
    _store_workspace,
    _workspace_precondition,
)
from app.services.ingestion.orchestration.stats import (
    _batch_stats,
    _fragment_stats,
    _workspace_stats,
)

__all__ = [
    "ARTIFACT_DIGEST_STATE_KEY",
    "ARTIFACT_NAME_STATE_KEY",
    "DOCUMENT_ID_STATE_KEY",
    "INGESTION_SIGNATURE_STATE_KEY",
    "SOURCE_CHUNKS_STATE_KEY",
    "VALIDATED_FINGERPRINT_STATE_KEY",
    "WORKSPACE_STATE_KEY",
    "_batch_payload",
    "_batch_stats",
    "_clear_validation_gate",
    "_current_provenance",
    "_fragment_stats",
    "_get_validation_service",
    "_get_workspace_service",
    "_load_workspace",
    "_persist_with_receipt",
    "_public_assessment",
    "_store_workspace",
    "_workspace_precondition",
    "_workspace_stats",
    "load_and_prepare_artifact_context",
]
