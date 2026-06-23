from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from open_subagent_mcp.config import Settings
from open_subagent_mcp.llm_client import FakeLLMClient
from open_subagent_mcp.mcp_server import SERVER_INSTRUCTIONS, create_server
from open_subagent_mcp.runtime import OpenSubagentRuntime


@pytest.mark.asyncio
async def test_spawn_rejects_unsupported_item_type(tmp_path: Path) -> None:
    runtime = OpenSubagentRuntime(settings=Settings(runs_dir=tmp_path / ".runs"), llm_client=FakeLLMClient([]))
    response = await runtime.spawn_agent(
        {
            "agent_type": "explorer",
            "message": "look",
            "cwd": str(tmp_path),
            "items": [{"type": "image", "path": "x.png"}],
        }
    )
    assert not response["ok"]
    assert response["error"]["code"] == "unsupported_item_type"


@pytest.mark.asyncio
async def test_spawn_invalid_agent_type_is_serializable(tmp_path: Path) -> None:
    runtime = OpenSubagentRuntime(settings=Settings(runs_dir=tmp_path / ".runs"), llm_client=FakeLLMClient([]))
    response = await runtime.spawn_agent(
        {
            "agent_type": "analysis",
            "message": "look",
            "cwd": str(tmp_path),
        }
    )
    assert not response["ok"]
    assert response["error"]["code"] == "invalid_request"
    assert response["error"]["details"]["hint"] == 'agent_type must be "explorer" or "worker"'
    json.dumps(response)


@pytest.mark.asyncio
async def test_spawn_long_timeout_requires_auth_and_is_serializable(tmp_path: Path) -> None:
    runtime = OpenSubagentRuntime(settings=Settings(runs_dir=tmp_path / ".runs"), llm_client=FakeLLMClient([]))
    response = await runtime.spawn_agent(
        {
            "agent_type": "explorer",
            "message": "look",
            "cwd": str(tmp_path),
            "timeout_seconds": 180,
        }
    )
    assert not response["ok"]
    assert response["error"]["code"] == "invalid_request"
    assert response["error"]["details"]["authorization_required"] == "long_running_commands"
    assert 'explicit_authorizations=["long_running_commands"]' in response["error"]["details"]["hint"]
    json.dumps(response)


@pytest.mark.asyncio
async def test_spawn_long_timeout_with_auth_passes_validation(tmp_path: Path) -> None:
    runtime = OpenSubagentRuntime(
        settings=Settings(runs_dir=tmp_path / ".runs"),
        llm_client=FakeLLMClient(
            [
                json.dumps(
                    {
                        "action": "finish",
                        "args": {
                            "status": "completed",
                            "summary": "ok",
                            "self_check_commands": [],
                            "tests": ["spawn validation"],
                            "risk_notes": ["fake"],
                            "open_issues": [],
                        },
                    }
                )
            ]
        ),
    )
    response = await runtime.spawn_agent(
        {
            "agent_type": "explorer",
            "message": "look",
            "cwd": str(tmp_path),
            "timeout_seconds": 180,
            "explicit_authorizations": ["long_running_commands"],
        }
    )
    assert response["ok"]
    waited = await runtime.wait_agent({"targets": [response["data"]["agent_id"]], "timeout_ms": 2000})
    assert waited["data"]["completed"][response["data"]["agent_id"]]["status"] == "completed"


def test_module_help_starts() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-m", "open_subagent_mcp", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0
    assert "Open Subagent MCP server" in result.stdout


def test_mcp_server_instructions_describe_trust_boundary() -> None:
    server = create_server()
    instructions = server.instructions
    assert instructions == SERVER_INSTRUCTIONS
    assert "local stdio MCP server" in instructions
    assert "blocks sensitive" in instructions
    assert "best-effort local file rollback" in instructions
    assert "MCP host or orchestrator remains responsible" in instructions
