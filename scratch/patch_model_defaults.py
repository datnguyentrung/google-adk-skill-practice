from pathlib import Path
repls = {
    Path(r"D:\Thuc_tap_MB\google-adk-skill-practice\app\services\ingestion\adk_graph_mapper.py"): [("gemini-3.5-flash-lite", "gemini-3.5-flash")],
    Path(r"D:\Thuc_tap_MB\google-adk-skill-practice\app\services\ingestion\graph_validation.py"): [("gemini-3.5-flash-lite", "gemini-3.5-flash")],
}
for p, pairs in repls.items():
    s = p.read_text(encoding="utf-8-sig")
    for old, new in pairs:
        if old not in s:
            raise SystemExit(f"missing {old} in {p}")
        s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("patched", p.name)