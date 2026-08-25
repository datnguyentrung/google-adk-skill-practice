from pathlib import Path
p=Path('app/tools/ingestion_tools.py')
s=p.read_text(encoding='utf-8')
old='''    artifact_name = f"ingestion-receipt-{artifact_stem}.json"\n    artifact_version = await tool_context.save_artifact(\n'''
new='''    receipt = {\n        **receipt,\n        "partialPersistence": bool(result.get("partialPersistence", False)),\n        "persistenceMode": result.get("persistenceMode", "strict"),\n        "readinessIssuesIgnored": result.get("readinessIssuesIgnored", []),\n    }\n    artifact_name = f"ingestion-receipt-{artifact_stem}.json"\n    artifact_version = await tool_context.save_artifact(\n'''
assert old in s
s=s.replace(old,new,1)
s=s.replace('''            "commitStatus": str(result.get("commitStatus", "committed")),\n''','''            "commitStatus": str(result.get("commitStatus", "committed")),\n            "persistenceMode": str(result.get("persistenceMode", "strict")),\n''',1)
p.write_text(s,encoding='utf-8')
print('patched partial receipt metadata')