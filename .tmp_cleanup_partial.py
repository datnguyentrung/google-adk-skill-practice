from pathlib import Path
p=Path(r"D:\Thuc_tap_MB\google-adk-skill-practice\app\tools\ingestion_tools.py")
s=p.read_text(encoding='utf-8')
start=s.index('AUTO_SKIP_VALIDATION_CODES = {')
end=s.index('INGESTION_SKILL_DIR =', start)
s=s[:start]+s[end:]
start=s.index('\ndef _auto_skip_candidate_chunks(')
end=s.index('\ndef _conflict_repair_instruction(', start)
s=s[:start]+s[end:]
p.write_text(s,encoding='utf-8')
print('CLEAN_OK')