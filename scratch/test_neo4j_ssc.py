import os
from pathlib import Path
from dotenv import dotenv_values
root = Path(r"D:\Thuc_tap_MB\google-adk-skill-practice")
vals = dotenv_values(root / ".env")
for key in ("NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE"):
    os.environ[key] = vals[key]
os.environ["NEO4J_URI"] = vals["NEO4J_URI"].replace("neo4j+s://", "neo4j+ssc://")
os.chdir(root)
from app.config.neo4j import Neo4jClient
try:
    Neo4jClient.connect().verify_connectivity()
    print("neo4j_ssc=ok")
finally:
    Neo4jClient.close_driver()