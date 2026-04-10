import os
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    AGENT_HOST: str = os.getenv("AGENT_HOST", "0.0.0.0")
    AGENT_PORT: int = int(os.getenv("AGENT_PORT", "8000"))
    OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    DATA_DIR: str = os.getenv("DATA_DIR", "./data")
    DB_PATH: str = os.path.join(os.getenv("DATA_DIR", "./data"), "agent.db")
    ADMIN_KEY: Optional[str] = os.getenv("ADMIN_KEY", None)

    class Config:
        env_file = ".env"

settings = Settings()

# Ensure data dir exists
os.makedirs(settings.DATA_DIR, exist_ok=True)
