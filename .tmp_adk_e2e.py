import base64
import json
import uuid
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8001"
APP = "app"
USER = "debug-user"
SID = str(uuid.uuid4())
DOC = next(Path("docs").glob("*FLEXI REWARDS.md"))

session = requests.post(
    f"{BASE}/apps/{APP}/users/{USER}/sessions",
    json={"sessionId": SID},
    timeout=30,
)
session.raise_for_status()
print("SESSION", SID, flush=True)

data = base64.b64encode(DOC.read_bytes()).decode("ascii")
payload = {
    "appName": APP,
    "userId": USER,
    "sessionId": SID,
    "newMessage": {
        "role": "user",
        "parts": [
            {
                "text": (
                    "Hãy ingest toàn bộ tài liệu đính kèm bằng ingestion skill, "
                    "persist vào Neo4j và chạy đến trạng thái terminal. Không dừng giữa chừng."
                )
            },
            {
                "inlineData": {
                    "data": data,
                    "mimeType": "text/markdown",
                    "displayName": DOC.name,
                }
            },
        ],
    },
}
resp = requests.post(f"{BASE}/run", json=payload, timeout=900)
print("STATUS", resp.status_code, flush=True)
Path(".adk_e2e_response.json").write_text(resp.text, encoding="utf-8")
Path(".adk_e2e_session.txt").write_text(SID, encoding="utf-8")
print(resp.text[-8000:], flush=True)
resp.raise_for_status()
