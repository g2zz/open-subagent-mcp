from __future__ import annotations

import sys
from pathlib import Path

import pytest

from open_subagent_mcp.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(runs_dir=tmp_path / ".runs")


@pytest.fixture
def python_cmd() -> str:
    return sys.executable


@pytest.fixture(autouse=True)
def clean_sensitive_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISIBLE_TEST_ENV", "1")
    monkeypatch.setenv("SECRET_TOKEN_FOR_TEST", "hidden")
