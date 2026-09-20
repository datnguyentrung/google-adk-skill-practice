"""Artifact loading and extraction context preparation helpers."""

import hashlib
import logging
from typing import Any

from app.services.ingestion.orchestration.state import (
    ARTIFACT_DIGEST_STATE_KEY,
    ARTIFACT_NAME_STATE_KEY,
    DOCUMENT_ID_STATE_KEY,
    INGESTION_SIGNATURE_STATE_KEY,
    SOURCE_CHUNKS_STATE_KEY,
    WORKSPACE_STATE_KEY,
    IngestionRuntime,
    _clear_validation_gate,
    _current_provenance,
    _delete_state,
    _get_context_service,
)

logger = logging.getLogger(__name__)


async def load_and_prepare_artifact_context(
    artifact_name: str,
    tool_context: IngestionRuntime,
) -> dict[str, Any]:
    """
    Đọc artifact nguồn và chuẩn bị ngữ cảnh extraction, lưu vào session state.

    Args:
        artifact_name: Tên artifact tài liệu nguồn.
        tool_context: Tool context của phiên.

    Returns:
        Payload mô tả nguồn, số chunk và phần ontology rút gọn.
    """
    logger.info(
        "[PHASE:PREPARE_CONTEXT_START] Func: prepare_extraction_context | Document: '%s'",
        artifact_name,
    )
    _clear_validation_gate(tool_context)
    _delete_state(tool_context, ARTIFACT_DIGEST_STATE_KEY)
    _delete_state(tool_context, ARTIFACT_NAME_STATE_KEY)
    _delete_state(tool_context, DOCUMENT_ID_STATE_KEY)
    _delete_state(tool_context, INGESTION_SIGNATURE_STATE_KEY)
    _delete_state(tool_context, SOURCE_CHUNKS_STATE_KEY)
    _delete_state(tool_context, WORKSPACE_STATE_KEY)

    try:
        artifact = await tool_context.load_artifact(filename=artifact_name)
        if artifact is None:
            logger.error(
                "[INGESTION_ERROR] Phase: PREPARE_CONTEXT | Func: prepare_extraction_context | "
                "Document: '%s' | Error: Artifact not found",
                artifact_name,
            )
            return {
                "success": False,
                "stage": "artifact_loading",
                "error": f"Artifact not found: {artifact_name}",
            }

        data: bytes
        mime_type: str | None = None
        if artifact.inline_data is not None:
            raw_data = artifact.inline_data.data
            mime_type = artifact.inline_data.mime_type
            if raw_data is None:
                logger.error(
                    "[INGESTION_ERROR] Phase: PREPARE_CONTEXT | Func: prepare_extraction_context | "
                    "Document: '%s' | Error: Artifact contains no binary data",
                    artifact_name,
                )
                return {
                    "success": False,
                    "stage": "artifact_loading",
                    "error": f"Artifact contains no binary data: {artifact_name}",
                }
            if isinstance(raw_data, bytes):
                data = raw_data
            elif isinstance(raw_data, bytearray):
                data = bytes(raw_data)
            else:
                logger.error(
                    "[INGESTION_ERROR] Phase: PREPARE_CONTEXT | Func: prepare_extraction_context | "
                    "Document: '%s' | Error: Unsupported artifact data type %s",
                    artifact_name,
                    type(raw_data).__name__,
                )
                return {
                    "success": False,
                    "stage": "artifact_loading",
                    "error": (
                        f"Unsupported artifact data type: {type(raw_data).__name__}"
                    ),
                }
        elif artifact.text is not None:
            data = artifact.text.encode("utf-8")
            mime_type = "text/plain"
        else:
            logger.error(
                "[INGESTION_ERROR] Phase: PREPARE_CONTEXT | Func: prepare_extraction_context | "
                "Document: '%s' | Error: Artifact does not contain supported inline data or text",
                artifact_name,
            )
            return {
                "success": False,
                "stage": "artifact_loading",
                "error": (
                    "Artifact does not contain supported inline data or text: "
                    f"{artifact_name}"
                ),
            }

        artifact_digest = hashlib.sha256(data).hexdigest()
        tool_context.state[ARTIFACT_DIGEST_STATE_KEY] = artifact_digest
        tool_context.state[ARTIFACT_NAME_STATE_KEY] = artifact_name

        context = _get_context_service().prepare_uploaded_document(
            filename=artifact_name,
            data=data,
            mime_type=mime_type,
        )
        if context.document_id:
            tool_context.state[DOCUMENT_ID_STATE_KEY] = context.document_id
        provenance = _current_provenance(tool_context)
        try:
            source_text = data.decode("utf-8")
            total_lines = len(source_text.splitlines())
            total_chars = len(source_text)
        except UnicodeDecodeError:
            total_lines = None
            total_chars = len(data)
        logger.info(
            "[PHASE:PREPARE_CONTEXT_SUCCESS] Func: prepare_extraction_context | "
            "Document: '%s' | TotalChars: %s | TotalLines: %s | Chunks: %s",
            artifact_name,
            total_chars,
            total_lines,
            len(context.chunks),
        )
        tool_context.state[SOURCE_CHUNKS_STATE_KEY] = [
            chunk.model_dump(by_alias=True, exclude_none=True)
            for chunk in context.chunks
        ]
        return {
            "success": True,
            "stage": "completed",
            "chunkCount": len(context.chunks),
            "documentId": provenance.document_id,
            "configSignature": provenance.config_signature,
            "ingestionSignature": provenance.ingestion_signature,
            "sourceVersionId": provenance.source_version_id,
            **context.model_dump(by_alias=True, exclude_none=True),
        }
    except Exception as exc:
        logger.exception(
            "[INGESTION_ERROR] Phase: PREPARE_CONTEXT | Func: prepare_extraction_context | "
            "Document: '%s'",
            artifact_name,
        )
        _clear_validation_gate(tool_context)
        _delete_state(tool_context, ARTIFACT_DIGEST_STATE_KEY)
        _delete_state(tool_context, ARTIFACT_NAME_STATE_KEY)
        _delete_state(tool_context, DOCUMENT_ID_STATE_KEY)
        _delete_state(tool_context, INGESTION_SIGNATURE_STATE_KEY)
        _delete_state(tool_context, SOURCE_CHUNKS_STATE_KEY)
        return {
            "success": False,
            "stage": "prepare_extraction_context",
            "error": str(exc),
        }
