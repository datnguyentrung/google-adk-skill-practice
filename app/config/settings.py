from typing import Optional
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    PROJECT_NAME: str = "Shopping Research Agent"
    API_V1: str = "/api/v1"

    # --- Các biến Database ---
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "shopping_research_agent"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = "postgres"

    # --- Các biến API Keys & Cloud ---
    GOOGLE_API_KEY: str = ""
    HF_TOKEN: str = ""
    ZAI_API_KEY: str = ""
    SERPER_API_KEY: str = ""
    VERTEX_ENGINE_ID: str = ""  # Đã sửa lại lỗi chính tả từ VERTX thành VERTEX
    PROJECT_ID: str = ""
    GOOGLE_APPLICATION_CREDENTIALS: str = ""
    GOOGLE_CLOUD_API_KEY: str = ""
    REDIS_URL: str = ""
    NGROK_URL: str = ""
    OPENROUTER_API_KEY: str = ""
    AGENTOPS_API_KEY: str = ""
    CLASSIFIER_MODEL_PATH: str = ""

    # --- Các biến bổ sung từ file .env ---
    DEBUG: bool = False
    API_VERSION: str = "v1"
    SEARCH_TIMEOUT: int = 30
    MAX_RESULTS_PER_QUERY: int = 50
    LOG_LEVEL: str = "INFO"
    ENABLE_SERPER_SEARCH: bool = False
    TRACE_ENABLED: Optional[str] = None  # Có thể để trống hoặc điền giá trị
    TRACE_STREAM: Optional[str] = None  # Có thể để trống hoặc điền giá trị

    # --- Supabase Auth ---
    SUPABASE_JWT_SECRET: str = ""  # JWT Secret từ Supabase Dashboard > Settings > API
    SUPABASE_URL: str = ""  # URL dự án Supabase của bạn, ví dụ: https://your-project-id.supabase.co
    SUPABASE_SERVICE_KEY: str = ""

    # --- Neo4j AuraDB ---
    NEO4J_URI: str = ""
    NEO4J_USERNAME: str = ""
    NEO4J_PASSWORD: str = ""
    NEO4J_DATABASE: str = ""

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_debug(cls, value):
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"release", "prod", "production"}:
                return False
            if normalized in {"debug", "dev", "development"}:
                return True
        return value

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")


# Khởi tạo một biến settings để import dùng ở mọi nơi
settings = Settings()
