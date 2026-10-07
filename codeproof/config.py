from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CODEPROOF_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://codeproof:codeproof@localhost:5432/codeproof"
    data_dir: Path = Path("data")
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    max_extracted_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    max_files: int = Field(default=2000, ge=1, le=10000)
    max_file_bytes: int = Field(default=512 * 1024, ge=1024)
    max_attempts: int = Field(default=3, ge=1, le=5)
    max_total_attempts: int = Field(default=12, ge=1, le=30)
    max_findings: int = Field(default=20, ge=1, le=100)
    max_run_seconds: int = Field(default=300, ge=10, le=1800)
    command_timeout: int = Field(default=30, ge=1, le=120)
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: int = Field(default=30, ge=1, le=120)
    llm_max_calls: int = Field(default=6, ge=0, le=20)
    llm_max_input_chars: int = Field(default=24000, ge=1000, le=100000)
    sandbox_enabled: bool = False
    sandbox_image: str = "codeproof-sandbox:local"
    sandbox_timeout: int = Field(default=60, ge=1, le=180)
    api_token: str = ""
    worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=30)
    max_queued_runs: int = Field(default=20, ge=1, le=100)


settings = Settings()
