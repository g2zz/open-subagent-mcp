from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from open_subagent_mcp.config import Settings
from open_subagent_mcp.llm_client import FakeLLMClient
from open_subagent_mcp.runtime import OpenSubagentRuntime


def finish(summary: str) -> str:
    return json.dumps(
        {
            "action": "finish",
            "args": {
                "status": "completed",
                "summary": summary,
                "self_check_commands": ["fake smoke"],
                "tests": ["fake runtime lifecycle"],
                "risk_notes": ["fake LLM only"],
                "open_issues": [],
            },
        }
    )


async def direct_runtime_smoke() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        settings = Settings(runs_dir=Path(tmp) / ".runs")
        fake = FakeLLMClient(
            [
                json.dumps(
                    {
                        "action": "write_file",
                        "args": {
                            "path": "first.txt",
                            "content": "first",
                            "mode": "create",
                            "reason": "fake smoke",
                        },
                    }
                ),
                finish("first segment complete"),
                json.dumps(
                    {
                        "action": "write_file",
                        "args": {
                            "path": "second.txt",
                            "content": "second",
                            "mode": "create",
                            "reason": "fake smoke segment two",
                        },
                    }
                ),
                finish("second segment complete"),
            ]
        )
        runtime = OpenSubagentRuntime(settings=settings, llm_client=fake)
        spawned = await runtime.spawn_agent(
            {"agent_type": "worker", "message": "create first file", "cwd": str(workspace)}
        )
        agent_id = spawned["data"]["agent_id"]
        waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 5000})
        assert waited["data"]["completed"][agent_id]["status"] == "completed"
        sent = await runtime.send_input({"target": agent_id, "message": "create second file"})
        segment_id = sent["data"]["current_segment_id"]
        waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 5000})
        assert waited["data"]["completed"][agent_id]["status"] == "completed"
        rolled = await runtime.rollback_agent({"agent_id": agent_id, "segment_id": segment_id})
        assert "second.txt" in rolled["data"]["reverted_files"]
        closed = await runtime.close_agent({"target": agent_id})
        assert closed["data"]["status"] == "closed"
        print(json.dumps({"ok": True, "mode": "direct_runtime_fake", "agent_id": agent_id}, ensure_ascii=False))


async def mcp_stdio_smoke() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        outputs = [
            json.dumps(
                {
                    "action": "write_file",
                    "args": {
                        "path": "mcp_first.txt",
                        "content": "first",
                        "mode": "create",
                        "reason": "mcp smoke",
                    },
                }
            ),
            finish("mcp smoke first segment complete"),
            json.dumps(
                {
                    "action": "write_file",
                    "args": {
                        "path": "mcp_second.txt",
                        "content": "second",
                        "mode": "create",
                        "reason": "mcp smoke second segment",
                    },
                }
            ),
            finish("mcp smoke second segment complete"),
        ]
        env = os.environ.copy()
        env["SUBAGENT_MCP_RUNS_DIR"] = str(Path(tmp) / ".runs")
        env["SUBAGENT_MCP_FAKE_LLM_OUTPUTS"] = json.dumps(outputs)
        default_command = Path(sys.executable).with_name("open-subagent-mcp")
        configured_command = os.environ.get("SUBAGENT_MCP_COMMAND")
        if configured_command:
            command = configured_command
            args = []
        elif default_command.exists():
            command = str(default_command)
            args = []
        else:
            command = sys.executable
            args = ["-m", "open_subagent_mcp"]
        params = StdioServerParameters(command=command, args=args, env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.instructions
                assert "local stdio MCP server" in init.instructions
                assert "OpenAI-compatible" in init.instructions
                spawned = await session.call_tool(
                    "subagent_spawn",
                    {"agent_type": "worker", "message": "create mcp file", "cwd": str(workspace)},
                )
                agent_id = spawned.structuredContent["data"]["agent_id"]
                waited = await session.call_tool(
                    "subagent_wait",
                    {"targets": [agent_id], "timeout_ms": 5000},
                )
                assert waited.structuredContent["data"]["completed"][agent_id]["status"] == "completed"
                sent = await session.call_tool(
                    "subagent_send_message",
                    {"target": agent_id, "message": "create second mcp file"},
                )
                segment_id = sent.structuredContent["data"]["current_segment_id"]
                waited = await session.call_tool(
                    "subagent_wait",
                    {"targets": [agent_id], "timeout_ms": 5000},
                )
                assert waited.structuredContent["data"]["completed"][agent_id]["status"] == "completed"
                rolled = await session.call_tool(
                    "subagent_rollback",
                    {"agent_id": agent_id, "segment_id": segment_id},
                )
                assert "mcp_second.txt" in rolled.structuredContent["data"]["reverted_files"]
                closed = await session.call_tool("subagent_close", {"target": agent_id})
                assert closed.structuredContent["data"]["status"] == "closed"
        print(json.dumps({"ok": True, "mode": "mcp_stdio_fake", "tools": 5}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(mcp_stdio_smoke())
