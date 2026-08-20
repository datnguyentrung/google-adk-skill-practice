from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    # --- AgentOps ---
    AGENTOPS_API_KEY: str = ""

    # --- Neo4j ---
    NEO4J_URI: str = ""
    NEO4J_USERNAME: str = ""
    NEO4J_PASSWORD: str = ""
    NEO4J_DATABASE: str = ""

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")


# Khởi tạo một biến settings để import dùng ở mọi nơi
settings = Settings()
