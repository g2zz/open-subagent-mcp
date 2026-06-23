from __future__ import annotations

import pytest

from open_subagent_mcp.config import load_settings


def test_default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_MODEL_NAME",
        "SUBAGENT_MCP_MAX_CONCURRENCY",
    ]:
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert settings.openai_base_url == "http://localhost:8000/v1"
    assert settings.openai_api_key == "YOUR_API_KEY"
    assert settings.openai_model_name == "openai-compatible-model"
    assert settings.max_concurrency == 8


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "http://example.test/v1/")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "model-x")
    monkeypatch.setenv("SUBAGENT_MCP_MAX_CONCURRENCY", "3")
    settings = load_settings()
    assert settings.openai_base_url == "http://example.test/v1"
    assert settings.openai_api_key == "sk-test"
    assert settings.openai_model_name == "model-x"
    assert settings.max_concurrency == 3


def test_invalid_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBAGENT_MCP_MAX_CONCURRENCY", "bad")
    with pytest.raises(ValueError):
        load_settings()


def test_invalid_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBAGENT_MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError):
        load_settings()
