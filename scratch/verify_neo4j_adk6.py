import os
from pathlib import Path
from dotenv import dotenv_values
ROOT = Path(r"D:\Thuc_tap_MB\google-adk-skill-practice")
vals = dotenv_values(ROOT / ".env")
for key in ("NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE"):
    os.environ[key] = vals[key]
uri = vals["NEO4J_URI"]
os.environ["NEO4J_URI"] = "neo4j+ssc://" + uri[len("neo4j+s://"):] if uri.startswith("neo4j+s://") else uri
os.chdir(ROOT)
from app.config.neo4j import Neo4jClient

driver = Neo4jClient.connect()
db = vals["NEO4J_DATABASE"]
with driver.session(database=db) as s:
    print("labels=", [dict(r) for r in s.run("MATCH (n) RETURN labels(n) AS labels, count(*) AS count ORDER BY count DESC")])
    print("rels=", [dict(r) for r in s.run("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY count DESC")])
    products = [dict(r) for r in s.run("MATCH (p:BankingProduct) RETURN keys(p) AS keys, properties(p) AS props LIMIT 3")]
    print("products=", products)
Neo4jClient.close_driver()