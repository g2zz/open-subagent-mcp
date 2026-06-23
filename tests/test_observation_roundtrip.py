from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_subagent_mcp.actions import observation_ok
from open_subagent_mcp.models import Observation

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def roundtrip(payload: dict) -> Observation:
    observation = observation_ok("act_roundtrip", payload)
    dumped = observation.model_dump_json()
    loaded = json.loads(dumped)
    return Observation.model_validate(loaded)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("readme", (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")),
        ("pyproject", (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")),
        ("json", '{"action":"finish","args":{"summary":"ok"}}'),
        (
            "toml-env",
            'env = { OPENAI_BASE_URL = "http://localhost:8000/v1", '
            'OPENAI_API_KEY = "YOUR_API_KEY", OPENAI_MODEL_NAME = "openai-compatible-model" }',
        ),
        ("chinese", "这是一次 MCP 接入测试，包含中文 observation。"),
        ("long-text", "line\n" * 5000),
        ("secret-like", "token=abc123456789 password=hunter2 credential=raw"),
    ],
)
def test_observation_roundtrip_for_realistic_content(name: str, content: str) -> None:
    observation = roundtrip({"name": name, "content": content})
    assert observation.ok is True
    assert observation.data is not None
    assert observation.data["name"] == name


def test_observation_roundtrip_redacts_without_breaking_json() -> None:
    observation = roundtrip({"content": 'OPENAI_API_KEY = "YOUR_API_KEY"'})
    dumped = observation.model_dump_json()
    assert "OPENAI_API_KEY" in dumped
    assert "YOUR_API_KEY" not in dumped
    json.loads(dumped)
