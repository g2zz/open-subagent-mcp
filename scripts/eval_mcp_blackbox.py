from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def finish(summary: str, risk: str = "mcp blackbox eval") -> str:
    return json.dumps(
        {
            "action": "finish",
            "args": {
                "status": "completed",
                "summary": summary,
                "self_check_commands": ["mcp blackbox eval"],
                "tests": ["mcp stdio tool lifecycle"],
                "risk_notes": [risk],
                "open_issues": [],
            },
        }
    )


def action(name: str, args: dict) -> str:
    return json.dumps({"action": name, "args": args})


async def main() -> None:
    eval_root = Path(tempfile.mkdtemp(prefix="open-subagent-mcp-mcp-blackbox-"))
    try:
        readonly = eval_root / "readonly"
        worker = eval_root / "worker"
        readonly.mkdir()
        worker.mkdir()
        shutil.copy2(PROJECT_ROOT / "README.md", readonly / "README.md")
        shutil.copy2(PROJECT_ROOT / "pyproject.toml", readonly / "pyproject.toml")

        outputs = [
            action("read_file", {"path": "README.md"}),
            finish("read README through MCP"),
            action("write_file", {"path": "first.txt", "content": "first", "mode": "create", "reason": "mcp eval"}),
            finish("worker first segment"),
            action("write_file", {"path": "second.txt", "content": "second", "mode": "create", "reason": "mcp eval second"}),
            finish("worker second segment"),
        ]
        env = os.environ.copy()
        env["SUBAGENT_MCP_RUNS_DIR"] = str(eval_root / ".runs")
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

                explorer = await session.call_tool(
                    "subagent_spawn",
                    {"agent_type": "explorer", "message": "Read README and finish.", "cwd": str(readonly), "max_steps": 6},
                )
                explorer_id = explorer.structuredContent["data"]["agent_id"]
                waited = await session.call_tool(
                    "subagent_wait",
                    {"targets": [explorer_id], "timeout_ms": 10000},
                )
                explorer_result = waited.structuredContent["data"]["completed"][explorer_id]
                assert explorer_result["status"] == "completed"
                assert explorer_result["changed_files"] == []
                assert explorer_result["commands_run"] == []
                await session.call_tool("subagent_close", {"target": explorer_id})

                spawned = await session.call_tool(
                    "subagent_spawn",
                    {"agent_type": "worker", "message": "Create first file.", "cwd": str(worker), "max_steps": 8},
                )
                worker_id = spawned.structuredContent["data"]["agent_id"]
                waited = await session.call_tool(
                    "subagent_wait",
                    {"targets": [worker_id], "timeout_ms": 10000},
                )
                assert waited.structuredContent["data"]["completed"][worker_id]["status"] == "completed"
                sent = await session.call_tool(
                    "subagent_send_message",
                    {"target": worker_id, "message": "Create second file."},
                )
                segment_id = sent.structuredContent["data"]["current_segment_id"]
                waited = await session.call_tool(
                    "subagent_wait",
                    {"targets": [worker_id], "timeout_ms": 10000},
                )
                assert waited.structuredContent["data"]["completed"][worker_id]["status"] == "completed"
                rolled = await session.call_tool(
                    "subagent_rollback",
                    {"agent_id": worker_id, "segment_id": segment_id},
                )
                assert "second.txt" in rolled.structuredContent["data"]["reverted_files"]
                assert (worker / "first.txt").exists()
                assert not (worker / "second.txt").exists()
                await session.call_tool("subagent_close", {"target": worker_id})

        print(json.dumps({"ok": True, "eval": "mcp_blackbox", "runs_dir": str(eval_root / ".runs")}, ensure_ascii=False))
        shutil.rmtree(eval_root)
    except Exception as exc:
        print(json.dumps({"ok": False, "eval": "mcp_blackbox", "archive_dir": str(eval_root), "error": str(exc)}, ensure_ascii=False))
        raise


if __name__ == "__main__":
    asyncio.run(main())
