from pathlib import Path
p=Path(r'D:\Thuc_tap_MB\google-adk-skill-practice\app\tools\ingestion_tools.py')
s=p.read_text(encoding='utf-8')
s=s.replace('''        if grounding_issues:\n            return _batch_validation_response(\n                workspace,\n                batch_index,\n                grounding_issues,\n            )\n''','''        if grounding_issues:\n            response = _batch_validation_response(\n                workspace, batch_index, grounding_issues\n            )\n            _pace_next_model_turn(tool_context)\n            return response\n''')
s=s.replace('''        if 0 <= batch_index < len(workspace.batches):\n            return _batch_validation_response(\n                workspace,\n                batch_index,\n                issues,\n                schema_error_locations=_schema_error_locations(exc),\n            )\n''','''        if 0 <= batch_index < len(workspace.batches):\n            response = _batch_validation_response(\n                workspace, batch_index, issues,\n                schema_error_locations=_schema_error_locations(exc),\n            )\n            _pace_next_model_turn(tool_context)\n            return response\n''')
s=s.replace('''    if next_batch is not None:\n        response["nextBatch"] = _batch_payload(workspace, next_batch)\n    return response\n''','''    if next_batch is not None:\n        response["nextBatch"] = _batch_payload(workspace, next_batch)\n    _pace_next_model_turn(tool_context)\n    return response\n''',1)
p.write_text(s,encoding='utf-8')
print('submit pacing added')
