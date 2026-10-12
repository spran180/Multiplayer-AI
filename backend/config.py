"""Environment-backed application settings."""
from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


# Read the repository-root .env for direct local launches. Docker Compose also
# injects these variables into the container, and existing shell variables win.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development").strip().lower()
    database_path: str = os.getenv("DATABASE_PATH", "data/state.sqlite3")
    redis_url: str = os.getenv("REDIS_URL", "")
    llm_provider: str = os.getenv("LLM_PROVIDER", "auto").strip().lower()
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
    ollama_timeout_seconds: int = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180"))
    max_agent_steps: int = int(os.getenv("MAX_AGENT_STEPS", "12"))
    approval_quorum: int = int(os.getenv("APPROVAL_QUORUM", "2"))
    approval_timeout_seconds: int = int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "300"))
    public_base_url: str = os.getenv(
        "PUBLIC_BASE_URL",
        os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000"),
    ).rstrip("/")


settings = Settings()
