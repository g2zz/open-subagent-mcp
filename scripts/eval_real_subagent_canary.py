from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from open_subagent_mcp.config import load_settings
from open_subagent_mcp.runtime import OpenSubagentRuntime

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def require_real_eval() -> bool:
    return os.getenv("RUN_REAL_LLM_EVAL") == "1"


async def wait_completed(runtime: OpenSubagentRuntime, agent_id: str, *, scenario: str) -> dict[str, Any]:
    waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 120000})
    assert waited["ok"], {"scenario": scenario, "waited": waited, "agent_id": agent_id}
    result = waited["data"]["completed"].get(agent_id)
    assert result is not None, {"scenario": scenario, "waited": waited, "agent_id": agent_id}
    return result


def action_names(run_dir: str) -> list[str]:
    path = Path(run_dir) / "actions.jsonl"
    if not path.exists():
        return []
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("action"):
            names.append(row["action"])
    return names


async def run_readonly(
    runtime: OpenSubagentRuntime,
    *,
    cwd: Path,
    message: str,
    expected_action: str | None = None,
    expected_actions: list[str] | None = None,
    scenario: str,
) -> str:
    spawned = await runtime.spawn_agent(
        {"agent_type": "explorer", "message": message, "cwd": str(cwd), "max_steps": 8}
    )
    assert spawned["ok"], {"scenario": scenario, "spawned": spawned}
    agent_id = spawned["data"]["agent_id"]
    result = await wait_completed(runtime, agent_id, scenario=scenario)
    try:
        assert result["status"] == "completed", {"scenario": scenario, "result": result, "agent_id": agent_id}
        assert result["changed_files"] == []
        assert result["commands_run"] == []
        actions = action_names(spawned["data"]["run_dir"])
        required_actions = expected_actions or ([expected_action] if expected_action else [])
        missing = [action for action in required_actions if action not in actions]
        assert not missing, {"scenario": scenario, "actions": actions, "missing": missing, "agent_id": agent_id}
        return agent_id
    finally:
        await runtime.close_agent({"target": agent_id})


async def run_worker_rollback(runtime: OpenSubagentRuntime, workspace: Path, *, scenario: str) -> str:
    spawned = await runtime.spawn_agent(
        {
            "agent_type": "worker",
            "message": (
                "Create canary.txt with content canary using write_file, then finish. "
                "Do not run commands."
            ),
            "cwd": str(workspace),
            "max_steps": 8,
        }
    )
    assert spawned["ok"], {"scenario": scenario, "spawned": spawned}
    agent_id = spawned["data"]["agent_id"]
    result = await wait_completed(runtime, agent_id, scenario=scenario)
    try:
        assert result["status"] == "completed", {"scenario": scenario, "result": result, "agent_id": agent_id}
        assert (workspace / "canary.txt").exists(), {"scenario": scenario, "agent_id": agent_id}
        rolled = await runtime.rollback_agent({"agent_id": agent_id})
        assert rolled["ok"], {"scenario": scenario, "rollback": rolled, "agent_id": agent_id}
        assert not (workspace / "canary.txt").exists(), {"scenario": scenario, "agent_id": agent_id}
        return agent_id
    finally:
        await runtime.close_agent({"target": agent_id})


async def main() -> None:
    if not require_real_eval():
        print(json.dumps({"ok": True, "skipped": True, "reason": "RUN_REAL_LLM_EVAL is not 1"}))
        return

    eval_root = Path(tempfile.mkdtemp(prefix="open-subagent-mcp-real-canary-"))
    try:
        settings = load_settings()
        settings.runs_dir = eval_root / ".runs"
        runtime = OpenSubagentRuntime(settings=settings)
        results: list[dict[str, Any]] = []

        for round_index in range(1, 4):
            readme_id = await run_readonly(
                runtime,
                cwd=PROJECT_ROOT,
                scenario=f"round_{round_index}_readme",
                expected_action="read_file",
                message=(
                    "只读 canary。先 read_file README.md。拿到 observation 后直接 finish，"
                    "不要运行命令，不要写文件。"
                ),
            )
            pyproject_id = await run_readonly(
                runtime,
                cwd=PROJECT_ROOT,
                scenario=f"round_{round_index}_pyproject",
                expected_action="read_file",
                message=(
                    "只读 canary。先 read_file pyproject.toml。拿到 observation 后直接 finish，"
                    "不要运行命令，不要写文件。"
                ),
            )
            search_id = await run_readonly(
                runtime,
                cwd=PROJECT_ROOT,
                scenario=f"round_{round_index}_search",
                expected_action="search",
                message=(
                    "只读 canary。先 search query 'Open Subagent MCP' path README.md。"
                    "拿到 observation 后直接 finish，不要运行命令，不要写文件。"
                ),
            )
            repo_map_id = await run_readonly(
                runtime,
                cwd=PROJECT_ROOT,
                scenario=f"round_{round_index}_repo_map_read_many",
                expected_actions=["repo_map", "read_many_files"],
                message=(
                    "只读 canary。第一步必须 repo_map，reason='map project'，path='.'，max_depth=1，max_entries=80。"
                    "拿到 observation 后第二步必须 read_many_files，reason='read key files'，"
                    "读取 README.md 和 pyproject.toml。拿到 observation 后直接 finish。"
                    "不要运行命令，不要写文件。"
                ),
            )
            worker_workspace = eval_root / f"worker-{round_index}"
            worker_workspace.mkdir()
            worker_id = await run_worker_rollback(
                runtime,
                worker_workspace,
                scenario=f"round_{round_index}_worker_rollback",
            )
            results.append(
                {
                    "round": round_index,
                    "readme": readme_id,
                    "pyproject": pyproject_id,
                    "search": search_id,
                    "repo_map_read_many": repo_map_id,
                    "worker": worker_id,
                }
            )

        print(
            json.dumps(
                {"ok": True, "eval": "real_subagent_canary", "rounds": 3, "results": results},
                ensure_ascii=False,
            )
        )
        if os.getenv("KEEP_EVAL_ARTIFACTS") != "1":
            shutil.rmtree(eval_root)
    except Exception as exc:
        print(json.dumps({"ok": False, "eval": "real_subagent_canary", "archive_dir": str(eval_root), "error": str(exc)}, ensure_ascii=False))
        raise


if __name__ == "__main__":
    asyncio.run(main())
