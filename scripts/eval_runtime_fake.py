from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from open_subagent_mcp.config import Settings
from open_subagent_mcp.llm_client import FakeLLMClient
from open_subagent_mcp.runtime import OpenSubagentRuntime

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def finish(summary: str, *, status: str = "completed", risk: str = "fake eval") -> str:
    return json.dumps(
        {
            "action": "finish",
            "args": {
                "status": status,
                "summary": summary,
                "self_check_commands": ["fake eval"],
                "tests": ["runtime fake eval"],
                "risk_notes": [risk],
                "open_issues": [],
            },
        }
    )


def action(name: str, args: dict[str, Any]) -> str:
    return json.dumps({"action": name, "args": args})


def prepare_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "README.md", workspace / "README.md")
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", workspace / "pyproject.toml")
    (workspace / "spec_excerpt.md").write_text(
        "# Spec Excerpt\n\nOpen Subagent MCP use MCP stdio and JSON action observations.\n",
        encoding="utf-8",
    )
    return workspace


async def run_agent(
    *,
    name: str,
    outputs: list[str],
    message: str,
    workspace: Path,
    settings: Settings,
    agent_type: str = "explorer",
    max_steps: int = 8,
) -> tuple[OpenSubagentRuntime, str, dict]:
    runtime = OpenSubagentRuntime(settings=settings, llm_client=FakeLLMClient(outputs))
    spawned = await runtime.spawn_agent(
        {
            "agent_type": agent_type,
            "message": message,
            "cwd": str(workspace),
            "max_steps": max_steps,
        }
    )
    assert spawned["ok"], {"scenario": name, "spawned": spawned}
    agent_id = spawned["data"]["agent_id"]
    waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 5000})
    assert waited["ok"], {"scenario": name, "waited": waited, "agent_id": agent_id}
    return runtime, agent_id, waited["data"]["completed"][agent_id]


async def main() -> None:
    eval_root = Path(tempfile.mkdtemp(prefix="open-subagent-mcp-runtime-fake-"))
    try:
        workspace = prepare_workspace(eval_root)
        results: list[dict[str, Any]] = []

        settings = Settings(runs_dir=eval_root / "runs-readme")
        _, agent_id, result = await run_agent(
            name="read_file_then_finish",
            outputs=[action("read_file", {"path": "README.md"}), finish("read README")],
            message="Read README.md once, then finish.",
            workspace=workspace,
            settings=settings,
        )
        assert result["status"] == "completed"
        assert result["changed_files"] == []
        assert result["commands_run"] == []
        results.append({"name": "read_file_then_finish", "agent_id": agent_id})

        settings = Settings(runs_dir=eval_root / "runs-list")
        _, agent_id, result = await run_agent(
            name="list_files_read_pyproject_finish",
            outputs=[
                action("list_files", {"path": "."}),
                action("read_file", {"path": "pyproject.toml"}),
                finish("listed files and read pyproject"),
            ],
            message="List files, read pyproject.toml, then finish.",
            workspace=workspace,
            settings=settings,
        )
        assert result["status"] == "completed"
        results.append({"name": "list_files_read_pyproject_finish", "agent_id": agent_id})

        settings = Settings(runs_dir=eval_root / "runs-search")
        _, agent_id, result = await run_agent(
            name="search_read_finish",
            outputs=[
                action("search", {"query": "Open Subagent MCP", "path": "."}),
                action("read_file", {"path": "spec_excerpt.md"}),
                finish("searched and read spec excerpt"),
            ],
            message="Search for Open Subagent MCP, read spec_excerpt.md, then finish.",
            workspace=workspace,
            settings=settings,
        )
        assert result["status"] == "completed"
        results.append({"name": "search_read_finish", "agent_id": agent_id})

        settings = Settings(runs_dir=eval_root / "runs-repair")
        _, agent_id, result = await run_agent(
            name="format_error_repair",
            outputs=["not json", finish("recovered after repair")],
            message="Finish after repairing invalid JSON.",
            workspace=workspace,
            settings=settings,
        )
        assert result["status"] == "completed"
        results.append({"name": "format_error_repair", "agent_id": agent_id})

        settings = Settings(runs_dir=eval_root / "runs-waiting")
        runtime, agent_id, result = await run_agent(
            name="waiting_input_then_send_input",
            outputs=[finish("need more input", status="waiting_input"), finish("continued after input")],
            message="Return waiting_input first.",
            workspace=workspace,
            settings=settings,
        )
        assert result["status"] == "waiting_input"
        sent = await runtime.send_input({"target": agent_id, "message": "continue now"})
        assert sent["ok"], {"scenario": "waiting_input_then_send_input", "sent": sent}
        waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 5000})
        assert waited["data"]["completed"][agent_id]["status"] == "completed"
        results.append({"name": "waiting_input_then_send_input", "agent_id": agent_id})

        settings = Settings(runs_dir=eval_root / "runs-max-steps")
        _, agent_id, result = await run_agent(
            name="max_steps_failure",
            outputs=[
                action("read_file", {"path": "README.md"}),
                action("read_file", {"path": "README.md"}),
            ],
            message="Keep reading until max steps.",
            workspace=workspace,
            settings=settings,
            max_steps=2,
        )
        assert result["status"] == "failed"
        assert result["failure_reason"] == "max_steps_exceeded"
        assert result["diagnostics"]["step_count"] == 2
        assert result["diagnostics"]["last_action"]["action"] == "read_file"
        results.append({"name": "max_steps_failure", "agent_id": agent_id})

        print(json.dumps({"ok": True, "eval": "runtime_fake", "results": results}, ensure_ascii=False))
        shutil.rmtree(eval_root)
    except Exception as exc:
        print(json.dumps({"ok": False, "eval": "runtime_fake", "archive_dir": str(eval_root), "error": str(exc)}, ensure_ascii=False))
        raise


if __name__ == "__main__":
    asyncio.run(main())
