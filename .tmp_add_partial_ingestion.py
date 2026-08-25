from pathlib import Path

ROOT = Path(r"D:\Thuc_tap_MB\google-adk-skill-practice")
P = ROOT / "app/tools/ingestion_tools.py"
s = P.read_text(encoding="utf-8")

anchor = 'DEFAULT_MAX_RETRIES_PER_BATCH = max(1, int(os.getenv("INGESTION_MAX_RETRIES_PER_BATCH", "3")))\n'
insert = '''DEFAULT_MAX_RETRIES_PER_BATCH = max(1, int(os.getenv("INGESTION_MAX_RETRIES_PER_BATCH", "3")))
AUTO_SKIP_VALIDATION_CODES = {
    "EVIDENCE_TEXT_NOT_IN_SOURCE",
    "PROPERTY_VALUE_NOT_GROUNDED",
    "COVERAGE_NOT_EVIDENCED",
    "EDGE_RELATION_NOT_GROUNDED",
    "UNCHANGED_RETRY",
}
CRITICAL_NON_SKIPPABLE_PROPERTIES = {
    "pskg:productCode",
    "pskg:bankingProductStatus",
    "pskg:bankingProductEffectiveFrom",
}
'''
assert anchor in s
s = s.replace(anchor, insert, 1)
P.write_text(s, encoding="utf-8")
P = ROOT / "app/tools/ingestion_tools.py"
s = P.read_text(encoding="utf-8")
anchor = '\ndef _conflict_repair_instruction(conflict: dict[str, Any]) -> str:\n'
helpers = r'''

def _auto_skip_candidate_chunks(response: dict[str, Any], fragment: GraphPatchFragment) -> list[int]:
    indexes = set(response.get("affectedChunkIndexes") or [])
    errors = response.get("errors") or []
    for error in errors:
        node_id = error.get("nodeTempId")
        property_name = error.get("propertyName")
        edge_name = error.get("edgeName")
        for node in fragment.nodes:
            if node_id and node.temp_id != node_id:
                continue
            for prop in node.properties:
                if property_name and prop.property_name != property_name:
                    continue
                indexes.update(item.chunk_index for item in prop.evidence)
        for edge in fragment.edges:
            if edge_name and edge.edge_name != edge_name:
                continue
            indexes.update(item.chunk_index for item in edge.evidence)
    if not indexes:
        indexes.update(
            item.chunk_index for item in fragment.coverage if item.decision == "MAPPED"
        )
    return sorted(indexes)


def _auto_skip_allowed(response: dict[str, Any], fragment: GraphPatchFragment, skip_indexes: list[int]) -> bool:
    codes = {item.get("code") for item in (response.get("errors") or []) if item.get("code")}
    codes.update(response.get("errorSummary", {}).get("codes", []))
    if codes and not codes <= AUTO_SKIP_VALIDATION_CODES:
        return False
    skip_set = set(skip_indexes)
    for node in fragment.nodes:
        for prop in node.properties:
            if prop.property_name not in CRITICAL_NON_SKIPPABLE_PROPERTIES:
                continue
            if any(item.chunk_index in skip_set for item in prop.evidence):
                return False
    return bool(skip_indexes)
'''
assert anchor in s
s = s.replace(anchor, helpers + anchor, 1)
P.write_text(s, encoding="utf-8")
P = ROOT / "app/tools/ingestion_tools.py"
s = P.read_text(encoding="utf-8")
anchor = '\ndef _conflict_repair_instruction(conflict: dict[str, Any]) -> str:\n'
more = r'''

def _sanitize_fragment_for_skipped_chunks(
    fragment: GraphPatchFragment,
    skip_indexes: list[int],
) -> GraphPatchFragment:
    skip_set = set(skip_indexes)
    sanitized = fragment.model_copy(deep=True)
    for node in sanitized.nodes:
        kept_properties = []
        for prop in node.properties:
            remaining = [item for item in prop.evidence if item.chunk_index not in skip_set]
            if remaining:
                prop.evidence = remaining
                kept_properties.append(prop)
        node.properties = kept_properties
        node.evidence = [item for item in node.evidence if item.chunk_index not in skip_set]

    sanitized.edges = [
        edge for edge in sanitized.edges
        if (remaining := [item for item in edge.evidence if item.chunk_index not in skip_set])
        and not setattr(edge, "evidence", remaining)
    ]
    referenced = {
        temp_id
        for edge in sanitized.edges
        for temp_id in (edge.source_temp_id, edge.target_temp_id)
    }
    sanitized.nodes = [
        node for node in sanitized.nodes if node.properties or node.temp_id in referenced
    ]
    valid_node_ids = {node.temp_id for node in sanitized.nodes}
    sanitized.edges = [
        edge for edge in sanitized.edges
        if edge.source_temp_id in valid_node_ids and edge.target_temp_id in valid_node_ids
    ]
    for item in sanitized.coverage:
        if item.chunk_index in skip_set:
            item.decision = "NOT_RELEVANT"
            item.reason = "Auto-skipped after repeated non-critical validation failure"
    sanitized.warnings = list(dict.fromkeys([
        *sanitized.warnings,
        *[f"SKIPPED_AFTER_RETRIES chunk={index}" for index in skip_indexes],
    ]))
    return sanitized


def _record_auto_skip(
    ingestion_id: str,
    batch_index: int,
    skip_indexes: list[int],
    response: dict[str, Any],
    tool_context: ToolContext,
) -> None:
    workspace = _load_workspace(tool_context)
    if workspace is None or workspace.ingestion_id != ingestion_id:
        return
    workspace.skipped_chunk_indexes = sorted(set([
        *workspace.skipped_chunk_indexes, *skip_indexes
    ]))
    warning = {
        "code": "SKIPPED_AFTER_RETRIES",
        "batchIndex": batch_index,
        "chunkIndexes": skip_indexes,
        "errorCodes": response.get("errorSummary", {}).get("codes", []),
        "errors": response.get("errors", [])[:10],
    }
    workspace.ingestion_warnings.append(warning)
    _store_workspace(tool_context, workspace)
    logger.warning(
        "INGESTION_AUTO_SKIP ingestion_id=%s batch=%s chunks=%s errors=%s",
        ingestion_id, batch_index, skip_indexes, warning["errorCodes"],
    )
'''
assert anchor in s
s = s.replace(anchor, more + anchor, 1)
P.write_text(s, encoding="utf-8")
P = ROOT / "app/tools/ingestion_tools.py"
s = P.read_text(encoding="utf-8")
anchor = '\ndef _conflict_repair_instruction(conflict: dict[str, Any]) -> str:\n'
more = r'''

def _try_auto_skip_failed_batch(
    *,
    ingestion_id: str,
    batch_index: int,
    fragment: GraphPatchFragment,
    response: dict[str, Any],
    tool_context: ToolContext,
) -> dict[str, Any] | None:
    skip_indexes = _auto_skip_candidate_chunks(response, fragment)
    if not _auto_skip_allowed(response, fragment, skip_indexes):
        return None
    sanitized = _sanitize_fragment_for_skipped_chunks(fragment, skip_indexes)
    fallback = submit_ingestion_batch(
        ingestion_id,
        batch_index,
        sanitized,
        tool_context,
    )
    if not fallback.get("success"):
        return None
    _record_auto_skip(
        ingestion_id,
        batch_index,
        skip_indexes,
        response,
        tool_context,
    )
    workspace = _load_workspace(tool_context)
    if workspace is not None:
        fallback["partial"] = True
        fallback["skippedChunks"] = workspace.skipped_chunk_indexes
        fallback["ingestionWarnings"] = workspace.ingestion_warnings
    return fallback
'''
assert anchor in s
s = s.replace(anchor, more + anchor, 1)
P.write_text(s, encoding="utf-8")
P = ROOT / "app/tools/ingestion_tools.py"
s = P.read_text(encoding="utf-8")
old = '''            if not response.get("retryRequired"):
                return {
                    **response,
                    "stage": "explicit_extraction_failure",
                    "terminal": True,
                    "ingestionId": ingestion_id,
                    "processedBatches": processed_batches,
                    "workspaceStats": (
                        _workspace_stats(workspace)
                        if (workspace := _load_workspace(tool_context)) is not None
                        else response.get("workspaceStats", {})
                    ),
                }
'''
new = '''            if not response.get("retryRequired"):
                fallback = _try_auto_skip_failed_batch(
                    ingestion_id=ingestion_id,
                    batch_index=batch_index,
                    fragment=fragment,
                    response=response,
                    tool_context=tool_context,
                )
                if fallback is not None:
                    response = fallback
                    processed_batches = int(response.get("processedBatches", 0))
                    break
                return {
                    **response,
                    "stage": "explicit_extraction_failure",
                    "terminal": True,
                    "ingestionId": ingestion_id,
                    "processedBatches": processed_batches,
                    "workspaceStats": (
                        _workspace_stats(workspace)
                        if (workspace := _load_workspace(tool_context)) is not None
                        else response.get("workspaceStats", {})
                    ),
                }
'''
assert old in s
s = s.replace(old, new, 1)
P.write_text(s, encoding="utf-8")
P = ROOT / "app/tools/ingestion_tools.py"
s = P.read_text(encoding="utf-8")
old = '''        else:
            return {
                **response,
                "success": False,
                "stage": "explicit_extraction_failure",
                "terminal": True,
                "ingestionId": ingestion_id,
                "batchIndex": batch_index,
                "processedBatches": processed_batches,
                "errors": response.get("errors", []),
                "message": (
                    f"Batch {batch_index} did not pass validation after "
                    f"{max_retries_per_batch} attempts"
                ),
                "workspaceStats": (
                    _workspace_stats(workspace)
                    if (workspace := _load_workspace(tool_context)) is not None
                    else response.get("workspaceStats", {})
                ),
            }
'''
new = '''        else:
            fallback = _try_auto_skip_failed_batch(
                ingestion_id=ingestion_id,
                batch_index=batch_index,
                fragment=fragment,
                response=response,
                tool_context=tool_context,
            )
            if fallback is not None:
                response = fallback
                processed_batches = int(response.get("processedBatches", 0))
                continue
            return {
                **response,
                "success": False,
                "stage": "explicit_extraction_failure",
                "terminal": True,
                "ingestionId": ingestion_id,
                "batchIndex": batch_index,
                "processedBatches": processed_batches,
                "errors": response.get("errors", []),
                "message": (
                    f"Batch {batch_index} did not pass validation after "
                    f"{max_retries_per_batch} attempts"
                ),
                "workspaceStats": (
                    _workspace_stats(workspace)
                    if (workspace := _load_workspace(tool_context)) is not None
                    else response.get("workspaceStats", {})
                ),
            }
'''
assert old in s
s = s.replace(old, new, 1)
P.write_text(s, encoding="utf-8")
P = ROOT / "app/tools/ingestion_tools.py"
s = P.read_text(encoding="utf-8")
s = s.replace(
'''CRITICAL_NON_SKIPPABLE_PROPERTIES = {
    "pskg:productCode",
    "pskg:bankingProductStatus",
    "pskg:bankingProductEffectiveFrom",
}
''',
'''CRITICAL_NON_SKIPPABLE_PROPERTIES = {
    "pskg:productCode",
    "pskg:bankingProductStatus",
    "pskg:bankingProductEffectiveFrom",
}
CRITICAL_NON_SKIPPABLE_EDGES = {"pskg:hasEligibilityRule"}
''',
1,
)
old = '''    for node in fragment.nodes:
        for prop in node.properties:
            if prop.property_name not in CRITICAL_NON_SKIPPABLE_PROPERTIES:
                continue
            if any(item.chunk_index in skip_set for item in prop.evidence):
                return False
    return bool(skip_indexes)
'''
new = '''    for node in fragment.nodes:
        for prop in node.properties:
            if prop.property_name not in CRITICAL_NON_SKIPPABLE_PROPERTIES:
                continue
            if any(item.chunk_index in skip_set for item in prop.evidence):
                return False
    for edge in fragment.edges:
        if edge.edge_name not in CRITICAL_NON_SKIPPABLE_EDGES:
            continue
        if any(item.chunk_index in skip_set for item in edge.evidence):
            return False
    return bool(skip_indexes)
'''
assert old in s
s = s.replace(old, new, 1)
P.write_text(s, encoding="utf-8")
P = ROOT / "app/tools/ingestion_tools.py"
s = P.read_text(encoding="utf-8")
old = '''        "candidateEdges": sum(
            len(batch.fragment.edges)
            for batch in workspace.batches
            if batch.fragment is not None
        ),
    }
'''
new = '''        "candidateEdges": sum(
            len(batch.fragment.edges)
            for batch in workspace.batches
            if batch.fragment is not None
        ),
        "skippedChunks": len(workspace.skipped_chunk_indexes),
        "warningCount": len(workspace.ingestion_warnings),
    }
'''
assert old in s
s = s.replace(old, new, 1)
P.write_text(s, encoding="utf-8")
