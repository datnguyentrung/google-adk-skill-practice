from pathlib import Path
p=Path('app/tools/ingestion_tools.py')
s=p.read_text(encoding='utf-8')
s=s.replace('''    return {\n        "success": success,\n        "stage": stage,\n        "terminal": True,\n        "commitStatus": commit_status,\n        "nodes": persisted_nodes,\n''','''    return {\n        "success": success,\n        "stage": stage,\n        "terminal": True,\n        "commitStatus": commit_status,\n        "partialPersistence": bool(result.get("partialPersistence", False)),\n        "persistenceMode": result.get("persistenceMode", "strict"),\n        "readinessIssuesIgnored": result.get("readinessIssuesIgnored", []),\n        "nodes": persisted_nodes,\n''',1)
s=s.replace('''    failure_message: str,\n) -> dict[str, Any]:\n''','''    failure_message: str,\n    allow_partial_persistence: bool = False,\n) -> dict[str, Any]:\n''',1)
s=s.replace('''        result = service.fill(graph_patch, artifact_digest, source_chunks)\n''','''        fill_kwargs = ({"allow_partial_persistence": True} if allow_partial_persistence else {})\n        result = service.fill(graph_patch, artifact_digest, source_chunks, **fill_kwargs)\n''',1)
p.write_text(s,encoding='utf-8')
print('patched receipt/persist helper')