from pathlib import Path

ROOT = Path(r"D:\Thuc_tap_MB\google-adk-skill-practice")


def patch(rel, old, new, count=1):
    path = ROOT / rel
    text = path.read_text(encoding="utf-8")
    assert old in text, f"missing anchor: {rel}"
    path.write_text(text.replace(old, new, count), encoding="utf-8")


patch(
    "app/tools/ingestion_tools.py",
    "import json\n",
    "import asyncio\nimport json\n",
)
patch(
    "app/tools/ingestion_tools.py",
    "from app.services.ingestion.fill_service import FillValidationError\n",
    "from app.services.ingestion.fill_service import FillValidationError\n"
    "from app.services.ingestion.fragment_grounding_repair import repair_fragment_grounding\n",
)
patch(
    "app/tools/ingestion_tools.py",
    'instructions.append("Use verbatim evidence text from the cited chunk.")',
    'instructions.append('\
    '"Use only exact verbatim evidence from the cited chunk. For Markdown tables, "'\
    '"copy the complete source row including pipe delimiters; never synthesize or "'\
    '"join multiple rows into one evidence string."'\
    ')',
)
patch(
    "app/tools/ingestion_tools.py",
    'def _conflict_repair_instruction(conflict: dict[str, Any]) -> str:\n    if conflict.get("propertyName") == "pskg:fee":',
    'def _conflict_repair_instruction(conflict: dict[str, Any]) -> str:\n'
    '    if conflict.get("propertyName") == "pskg:bankingProductName":\n'
    '        return (\n'
    '            "Prefer an explicit product-name field such as the Markdown row "\n'
    '            "| Tên sản phẩm | ... |. Never derive bankingProductName from "\n'
    '            "Tên tài liệu or the document title. Omit the weaker metadata-derived "\n'
    '            "name when an explicit product-name source exists."\n'
    '        )\n'
    '    if conflict.get("propertyName") == "pskg:fee":',
)
patch(
    "app/tools/ingestion_tools.py",
    '''                fragment = extractor.extract_fragment(
                    batch_payload=batch_payload,
                    ontology_catalog=ontology_catalog,
                    previous_error=previous_error,
                )''',
    '''                fragment = extractor.extract_fragment(
                    batch_payload=batch_payload,
                    ontology_catalog=ontology_catalog,
                    previous_error=previous_error,
                )
                batch_chunks = [
                    DocumentChunk.model_validate(item)
                    for item in batch_payload.get("chunks", [])
                ]
                fragment = repair_fragment_grounding(
                    fragment,
                    batch_chunks,
                    _get_validation_service().source_grounding,
                )''',
)
patch(
    "app/tools/ingestion_tools.py",
    '                    "affectedChunkIndexes",\n                )',
    '                    "affectedChunkIndexes",\n                    "conflict",\n                )',
)
patch(
    "app/tools/ingestion_tools.py",
    "                        time.sleep(delay)",
    "                        await asyncio.sleep(delay)",
)
patch(
    "app/services/ingestion/orchestrator.py",
    '''            "For pskg:productAttributes, always emit a JSON list, even when "
            "there is only one value. Use it only for product-specific attributes "
            "such as card tier, currency, channel, or capability; keep values atomic "
            "and source-grounded.\\n"
''',
    '''            "For pskg:productAttributes, always emit a JSON list, even when "
            "there is only one value. Use it only for product-specific attributes "
            "such as card tier, currency, channel, or capability; keep values atomic "
            "and source-grounded. Every list item must be supported by at least one "
            "verbatim evidence row. For Markdown tables, copy the complete original "
            "row including leading/trailing pipe delimiters. Never synthesize a "
            "semicolon-joined evidence sentence from multiple rows.\\n"
''',
)
patch(
    "app/services/ingestion/orchestrator.py",
    '''            "a segment code and do not coerce the phrase into productAttributes.\\n"
            "Do not stuff independent fee, interest, penalty, annual-fee, "
''',
    '''            "a segment code and do not coerce the phrase into productAttributes.\\n"
            "Map pskg:bankingProductName only from an explicit product-name field "
            "such as | Tên sản phẩm | ... |. Never infer bankingProductName from "
            "| Tên tài liệu | ... | or from the document title.\\n"
            "Do not stuff independent fee, interest, penalty, annual-fee, "
''',
)
patch(
    "app/services/ingestion/staged_ingestion.py",
    '''                    if cls._stable_value(current.value) != cls._stable_value(prop.value):
                        raise WorkspaceConflictError(
''',
    '''                    if cls._stable_value(current.value) != cls._stable_value(prop.value):
                        if prop.property_name == "pskg:bankingProductName":
                            current_priority = cls._property_evidence_priority(
                                prop.property_name, current.evidence
                            )
                            incoming_priority = cls._property_evidence_priority(
                                prop.property_name, prop.evidence
                            )
                            if incoming_priority > current_priority:
                                current.value = prop.value
                                current.evidence = cls._dedupe_models(prop.evidence)
                                continue
                            if current_priority > incoming_priority:
                                continue
                        raise WorkspaceConflictError(
''',
)
patch(
    "app/services/ingestion/staged_ingestion.py",
    '''    @staticmethod
    def _stable_value(value) -> str:
''',
    '''    @staticmethod
    def _property_evidence_priority(property_name: str, evidence: list[Evidence]) -> int:
        if property_name != "pskg:bankingProductName":
            return 0
        score = 0
        for item in evidence:
            text = item.text.casefold()
            section = (item.section or "").casefold()
            if "tên sản phẩm" in text:
                score = max(score, 20)
            elif "tên tài liệu" in text or "thông tin tài liệu" in section:
                score = max(score, 0)
            else:
                score = max(score, 10)
        return score

    @staticmethod
    def _stable_value(value) -> str:
''',
)
