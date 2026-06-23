from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_IGNORE_DIRS = (
    ".git,.hg,.svn,.runs,__pycache__,.pytest_cache,.mypy_cache,"
    ".ruff_cache,node_modules,dist,build,target"
)


class Settings(BaseModel):
    openai_base_url: str = "http://localhost:8000/v1"
    openai_api_key: str = "YOUR_API_KEY"
    openai_model_name: str = "openai-compatible-model"
    max_concurrency: int = Field(default=8, ge=1, le=64)
    default_command_timeout_seconds: int = Field(default=120, ge=1)
    max_steps: int = Field(default=80, ge=1, le=200)
    log_truncate_chars: int = Field(default=20000, ge=100)
    runs_dir: Path = Path(__file__).resolve().parents[2] / ".runs"
    sensitive_path_patterns: list[str] = Field(default_factory=list)
    snapshot_ignore_dirs: list[str] = Field(default_factory=list)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def load_settings() -> Settings:
    ignore_dirs = os.getenv("SUBAGENT_MCP_SNAPSHOT_IGNORE_DIRS", DEFAULT_IGNORE_DIRS)
    sensitive = os.getenv("SUBAGENT_MCP_SENSITIVE_PATH_PATTERNS", "")
    settings = Settings(
        openai_base_url=os.getenv("OPENAI_BASE_URL", Settings().openai_base_url).rstrip("/"),
        openai_api_key=os.getenv("OPENAI_API_KEY", Settings().openai_api_key),
        openai_model_name=os.getenv("OPENAI_MODEL_NAME", Settings().openai_model_name),
        max_concurrency=_int_env("SUBAGENT_MCP_MAX_CONCURRENCY", 8),
        default_command_timeout_seconds=_int_env(
            "SUBAGENT_MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS", 120
        ),
        max_steps=_int_env("SUBAGENT_MCP_MAX_STEPS", 80),
        log_truncate_chars=_int_env("SUBAGENT_MCP_LOG_TRUNCATE_CHARS", 20000),
        runs_dir=Path(
            os.getenv(
                "SUBAGENT_MCP_RUNS_DIR",
                str(Path(__file__).resolve().parents[2] / ".runs"),
            )
        ),
        sensitive_path_patterns=[p for p in sensitive.split(",") if p],
        snapshot_ignore_dirs=[p for p in ignore_dirs.split(",") if p],
    )
    if settings.default_command_timeout_seconds > 24 * 60 * 60:
        raise ValueError("SUBAGENT_MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS is too large")
    return settings
