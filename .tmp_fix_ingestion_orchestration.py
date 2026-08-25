from pathlib import Path

ROOT = Path(r"D:\Thuc_tap_MB\google-adk-skill-practice")


def load(rel):
    path = ROOT / rel
    return path, path.read_text(encoding="utf-8")


# productAttributes is multi-valued.
p, s = load("app/services/ingestion/staged_ingestion.py")
if 'MULTI_VALUE_PROPERTY_NAMES = {"pskg:productAttributes"}' not in s:
    anchor = "logger = logging.getLogger(__name__)\n\nMAX_BATCH_CHUNKS"
    assert anchor in s
    s = s.replace(
        anchor,
        'logger = logging.getLogger(__name__)\n\n'
        'MULTI_VALUE_PROPERTY_NAMES = {"pskg:productAttributes"}\n\n'
        'MAX_BATCH_CHUNKS',
        1,
    )
old = '''                    if isinstance(current.value, list) and isinstance(
                        prop.value,
                        list,
                    ):
                        current.value = cls._merge_list_values(
                            current.value,
                            prop.value,
                        )
                        current.evidence = cls._dedupe_models(
                            [*current.evidence, *prop.evidence]
                        )
                        continue'''
new = '''                    if prop.property_name in MULTI_VALUE_PROPERTY_NAMES:
                        current_values = current.value if isinstance(current.value, list) else [current.value]
                        incoming_values = prop.value if isinstance(prop.value, list) else [prop.value]
                        current.value = cls._merge_list_values(current_values, incoming_values)
                        current.evidence = cls._dedupe_models(
                            [*current.evidence, *prop.evidence]
                        )
                        continue
                    if isinstance(current.value, list) and isinstance(prop.value, list):
                        current.value = cls._merge_list_values(current.value, prop.value)
                        current.evidence = cls._dedupe_models(
                            [*current.evidence, *prop.evidence]
                        )
                        continue'''
assert old in s
p.write_text(s.replace(old, new, 1), encoding="utf-8")

# Validate one batch before it participates in cross-batch merge.
p, s = load("app/tools/ingestion_tools.py")
old = '''        candidate = _get_workspace_service().submit(
            workspace,
            batch_index,
            fragment,
        )
        batch = workspace.batches[batch_index]
'''
assert old in s
s = s.replace(old, '''        batch = workspace.batches[batch_index]
''', 1)
old = '''            _pace_next_model_turn(tool_context)
            return response
        candidate.retry_states.pop(str(batch_index), None)
        workspace = candidate
'''
new = '''            _pace_next_model_turn(tool_context)
            return response
        candidate = _get_workspace_service().submit(
            workspace,
            batch_index,
            fragment,
        )
        candidate.retry_states.pop(str(batch_index), None)
        workspace = candidate
'''
assert old in s
s = s.replace(old, new, 1)
old = '''def get_ingestion_tools() -> list:
    return list(INGESTION_TOOLS.values())
'''
new = '''USER_FACING_INGESTION_TOOL_ROLES = (
    "ingest_document_end_to_end",
    "get_ingestion_status",
    "validate_graph_patch",
    "fill_graph_patch",
)


def get_ingestion_tools() -> list:
    return [INGESTION_TOOLS[role] for role in USER_FACING_INGESTION_TOOL_ROLES]
'''
assert old in s
p.write_text(s.replace(old, new, 1), encoding="utf-8")
# Extractor semantic guidance.
p, s = load("app/services/ingestion/orchestrator.py")
old = '''            "For multi-row tables such as card type, card tier, and settlement "
            "currency, do not paraphrase evidence. Prefer list-valued "
            "pskg:productAttributes like [\\"Thẻ tín dụng cá nhân\\", \\"Gold\\", "
            "\\"VND\\"] with one verbatim evidence row per item.\\n"
'''
new = '''            "For pskg:productAttributes, always emit a JSON list, even when "
            "there is only one value. Use it only for product-specific attributes "
            "such as card tier, currency, channel, or capability; keep values atomic "
            "and source-grounded.\\n"
            "Do not put customer audience or segment phrases such as 'Dành cho "
            "khách hàng cá nhân' into pskg:productAttributes. Use "
            "pskg:CustomerSegment/pskg:targetsSegment only when the source provides "
            "enough identity and relationship evidence; otherwise do not fabricate "
            "a segment code and do not coerce the phrase into productAttributes.\\n"
'''
assert old in s
p.write_text(s.replace(old, new, 1), encoding="utf-8")

# Root skill only sees safe user-facing ingestion tools.
p, s = load("app/skills/ingestion/SKILL.md")
old = '''  adk_additional_tools:
    - ingest_document_end_to_end
    - begin_ingestion
    - submit_ingestion_batch
    - finalize_ingestion
    - fill_ingestion
    - get_ingestion_status
    - prepare_extraction_context
    - validate_graph_patch
    - fill_graph_patch
'''
new = '''  adk_additional_tools:
    - ingest_document_end_to_end
    - get_ingestion_status
    - validate_graph_patch
    - fill_graph_patch
'''
assert old in s
s = s.replace(old, new, 1)
anchor = '''manual tools for a normal user ingestion request unless the user explicitly asks
to debug or manually inspect batches. Do not fall back to a manual begin/submit
loop after a retryable validation error; the end-to-end tool owns retry pacing.
'''
replacement = '''manual tools for a normal user ingestion request unless the user explicitly asks
to debug or manually inspect batches. The root agent exposes only the end-to-end
long-document ingestion tool; staged begin/submit/finalize/fill helpers are
internal implementation/debug APIs and must not be emulated across model turns.
Do not fall back to a manual begin/submit loop after a retryable validation
error; the end-to-end tool owns retry pacing and retries.
'''
assert anchor in s
p.write_text(s.replace(anchor, replacement, 1), encoding="utf-8")

p, s = load("app/prompts/root_agent_prompt.md")
anchor = '''8. Continue through the selected skill's terminal state within the invocation;
   do not stop merely because another workflow step remains.
'''
replacement = '''8. Continue through the selected skill's terminal state within the invocation;
   do not stop merely because another workflow step remains. When a loaded skill
   exposes an end-to-end tool for a normal workflow, call that tool instead of
   recreating its internal staged steps across separate model turns.
'''
assert anchor in s
p.write_text(s.replace(anchor, replacement, 1), encoding="utf-8")
# Tests: scalar/list multi-value merge.
p, s = load("tests/ingestion/test_staged_ingestion.py")
anchor = '''    assert attributes.value == ["Gold", "VND", "Cashback"]
    assert len(attributes.evidence) == 2


def test_raw_repeated_scalar_fee_still_conflicts_with_details():
'''
insert = '''    assert attributes.value == ["Gold", "VND", "Cashback"]
    assert len(attributes.evidence) == 2


def test_product_attributes_scalar_and_list_merge_without_conflict():
    service = IngestionWorkspaceService()
    workspace = service.begin(artifact_name="long.md", provenance=provenance(), chunks=chunks(10))
    workspace = service.submit(workspace, 0, attributes_fragment(workspace.batches[0], "Gold"))
    workspace = service.submit(workspace, 1, attributes_fragment(workspace.batches[1], ["VND", "Cashback"]))
    patch = service.merged_patch(workspace)
    attributes = patch.nodes[0].properties[0]
    assert attributes.value == ["Gold", "VND", "Cashback"]
    assert len(attributes.evidence) == 2


def test_raw_repeated_scalar_fee_still_conflicts_with_details():
'''
assert anchor in s
p.write_text(s.replace(anchor, insert, 1), encoding="utf-8")

p, s = load("tests/ingestion/test_ingestion_skill.py")
old = '''from app.tools.ingestion_tools import INGESTION_TOOLS

INGESTION_TOOL_NAMES = {
    tool.__name__ for tool in INGESTION_TOOLS.values()
}
'''
new = '''from app.tools.ingestion_tools import get_ingestion_tools

INGESTION_TOOL_NAMES = {tool.__name__ for tool in get_ingestion_tools()}
'''
assert old in s
s = s.replace(old, new, 1)
anchor = '''        assert declared == INGESTION_TOOL_NAMES
        assert "$" not in skill.instructions
'''
replacement = '''        assert declared == INGESTION_TOOL_NAMES
        assert {
            "begin_ingestion",
            "submit_ingestion_batch",
            "finalize_ingestion",
            "fill_ingestion",
            "prepare_extraction_context",
        }.isdisjoint(declared)
        assert "$" not in skill.instructions
'''
assert anchor in s
p.write_text(s.replace(anchor, replacement, 1), encoding="utf-8")

p, s = load("tests/ingestion/test_ingestion_orchestrator.py")
old = '''    assert "Prefer list-valued pskg:productAttributes" in prompt
    assert "one verbatim evidence row per item" in prompt
'''
new = '''    assert "always emit a JSON list" in prompt
    assert "customer audience or segment phrases" in prompt
    assert "Dành cho khách hàng cá nhân" in prompt
    assert "pskg:CustomerSegment/pskg:targetsSegment" in prompt
'''
assert old in s
p.write_text(s.replace(old, new, 1), encoding="utf-8")

print("PATCH_OK")
