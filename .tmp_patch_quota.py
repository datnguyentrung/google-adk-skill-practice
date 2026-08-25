from pathlib import Path
p=Path(r'D:\Thuc_tap_MB\google-adk-skill-practice\app\tools\ingestion_tools.py')
s=p.read_text(encoding='utf-8')
s=s.replace('import logging\nimport re\n', 'import logging\nimport os\nimport re\nimport time\n')
s=s.replace('WORKSPACE_STATE_KEY = "temp:ingestion_workspace"\n', 'WORKSPACE_STATE_KEY = "temp:ingestion_workspace"\nBATCH_PACE_SECONDS = float(os.getenv("INGESTION_BATCH_PACE_SECONDS", "10"))\n')
s=s.replace('def _clear_validation_gate(tool_context: ToolContext) -> None:\n    _delete_state(tool_context, VALIDATED_FINGERPRINT_STATE_KEY)\n', 'def _clear_validation_gate(tool_context: ToolContext) -> None:\n    _delete_state(tool_context, VALIDATED_FINGERPRINT_STATE_KEY)\n\n\ndef _pace_next_model_turn(tool_context: ToolContext) -> None:\n    """Space expensive Gemini turns to stay below per-minute input quotas."""\n    if BATCH_PACE_SECONDS > 0 and isinstance(tool_context, ToolContext):\n        time.sleep(BATCH_PACE_SECONDS)\n')
s=s.replace('        "nextAction": "correct_and_resubmit_same_batch",\n        "nextBatch": _batch_payload(workspace, batch),\n', '        "nextAction": "correct_and_resubmit_same_batch",\n')
p.write_text(s,encoding='utf-8')
print('quota imports/pacer/retry payload updated')
