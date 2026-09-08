from pathlib import Path
p = Path(r"D:\Thuc_tap_MB\google-adk-skill-practice\scratch\persist_adk6_capture.py")
s = p.read_text(encoding="utf-8")
needle = '    os.environ.setdefault(key.strip(), value.strip().strip(\'"\').strip("\'"))\n\nfrom app.config.neo4j import Neo4jClient\n'
replace = '    os.environ.setdefault(key.strip(), value.strip().strip(\'"\').strip("\'"))\n\nuri = os.environ.get("NEO4J_URI", "")\nif uri.startswith("neo4j+s://"):\n    os.environ["NEO4J_URI"] = "neo4j+ssc://" + uri[len("neo4j+s://"):]\n\nfrom app.config.neo4j import Neo4jClient\n'
assert needle in s
p.write_text(s.replace(needle, replace), encoding="utf-8")
print("patched process-local ssc override")