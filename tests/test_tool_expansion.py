from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_subagent_mcp.actions import ActionExecutor, parse_action
from open_subagent_mcp.config import Settings
from open_subagent_mcp.llm_client import FakeLLMClient
from open_subagent_mcp.locks import RepositoryLockManager
from open_subagent_mcp.models import AgentStatus, AgentType, RunState
from open_subagent_mcp.runtime import OpenSubagentRuntime
from open_subagent_mcp.workspace import utc_now


def make_state(tmp_path: Path, run_dir: Path, *, agent_type: AgentType = AgentType.worker) -> RunState:
    now = utc_now()
    return RunState(
        agent_id="run_test",
        status=AgentStatus.running,
        agent_type=agent_type,
        cwd=str(tmp_path),
        run_dir=str(run_dir),
        current_segment_id="seg_0001",
        model="fake",
        created_at=now,
        updated_at=now,
    )


def executor(settings: Settings) -> ActionExecutor:
    return ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})


def finish(summary: str = "done") -> str:
    return json.dumps(
        {
            "action": "finish",
            "args": {
                "status": "completed",
                "summary": summary,
                "self_check_commands": [],
                "tests": ["fake"],
                "risk_notes": ["fake"],
                "open_issues": [],
            },
        }
    )


@pytest.mark.asyncio
async def test_read_many_files_partial_success_and_truncation(tmp_path: Path, settings: Settings) -> None:
    (tmp_path / "a.txt").write_text("alpha\nbeta", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x" * 200, encoding="utf-8")
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = make_state(tmp_path, run_dir, agent_type=AgentType.explorer)
    parsed = parse_action(
        json.dumps(
            {
                "action": "read_many_files",
                "args": {
                    "reason": "collect context",
                    "files": [{"path": "a.txt"}, {"path": "missing.txt"}, {"path": "b.txt"}],
                    "total_max_chars": 100,
                },
            }
        )
    )
    obs = await executor(settings).execute(state, parsed)
    assert obs.ok
    assert obs.truncated
    files = obs.data["files"]
    assert files[0]["ok"] is True
    assert files[1]["ok"] is False
    assert files[2]["truncated"] is True


@pytest.mark.asyncio
async def test_read_many_files_blocks_sensitive_path(tmp_path: Path, settings: Settings) -> None:
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = make_state(tmp_path, run_dir, agent_type=AgentType.explorer)
    parsed = parse_action(
        json.dumps(
            {
                "action": "read_many_files",
                "args": {"reason": "try sensitive", "files": [{"path": ".env"}]},
            }
        )
    )
    obs = await executor(settings).execute(state, parsed)
    assert obs.ok
    assert obs.data["files"][0]["ok"] is False
    assert obs.data["files"][0]["error"]["code"] == "sensitive_path_blocked"


@pytest.mark.asyncio
async def test_repo_map_skips_noise_and_reports_key_files(tmp_path: Path, settings: Settings) -> None:
    (tmp_path / "README.md").write_text("# hi", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('x')", encoding="utf-8")
    outside = tmp_path.parent / "outside-repo-map.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "escape").symlink_to(outside)
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = make_state(tmp_path, run_dir, agent_type=AgentType.explorer)
    parsed = parse_action(json.dumps({"action": "repo_map", "args": {"reason": "map", "max_depth": 3}}))
    obs = await executor(settings).execute(state, parsed)
    assert obs.ok
    tree_paths = {row["path"] for row in obs.data["tree"]}
    assert "README.md" in tree_paths
    assert ".venv" not in tree_paths
    assert "src/main.py" in obs.data["entry_candidates"]
    assert any(row["path"] == "pyproject.toml" for row in obs.data["key_files"])
    assert "escape" in obs.data["symlink_escapes_skipped"]


@pytest.mark.asyncio
async def test_run_tests_records_command_logs_and_effects(tmp_path: Path, settings: Settings, python_cmd: str) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = make_state(tmp_path, run_dir, agent_type=AgentType.worker)
    parsed = parse_action(
        json.dumps(
            {
                "action": "run_tests",
                "args": {
                    "reason": "verify",
                    "cmd": f"{python_cmd} -c \"from pathlib import Path; Path('test.out').write_text('ok'); print('pass')\"",
                    "test_type": "custom",
                },
            }
        )
    )
    obs = await executor(settings).execute(state, parsed)
    assert obs.ok
    assert obs.data["test_type"] == "custom"
    assert obs.data["returncode"] == 0
    assert Path(obs.data["stdout_path"]).exists()
    assert (tmp_path / "test.out").exists()
    effects = [json.loads(line) for line in (run_dir / "command_effects.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["path"] == "test.out" for row in effects)


@pytest.mark.asyncio
async def test_run_tests_requires_worker(tmp_path: Path, settings: Settings, python_cmd: str) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = make_state(tmp_path, run_dir, agent_type=AgentType.explorer)
    parsed = parse_action(
        json.dumps(
            {
                "action": "run_tests",
                "args": {"reason": "verify", "cmd": f"{python_cmd} -c \"print(1)\""},
            }
        )
    )
    obs = await executor(settings).execute(state, parsed)
    assert not obs.ok
    assert obs.error.code.value == "action_not_allowed"


@pytest.mark.asyncio
async def test_use_skill_context_reads_text_item_and_truncates(tmp_path: Path) -> None:
    runtime = OpenSubagentRuntime(
        settings=Settings(runs_dir=tmp_path / ".runs"),
        llm_client=FakeLLMClient(
            [
                json.dumps(
                    {
                        "action": "use_skill_context",
                        "args": {"reason": "read skill first", "name": "skill:demo", "max_chars": 120},
                    }
                ),
                finish("used context"),
            ]
        ),
    )
    response = await runtime.spawn_agent(
        {
            "agent_type": "explorer",
            "message": "use context",
            "cwd": str(tmp_path),
            "items": [{"type": "text", "name": "skill:demo", "text": "A" * 200}],
        }
    )
    agent_id = response["data"]["agent_id"]
    waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 2000})
    result = waited["data"]["completed"][agent_id]
    assert result["status"] == "completed"
    assert result["items_path"]
    rows = [json.loads(line) for line in Path(result["items_path"]).read_text(encoding="utf-8").splitlines()]
    assert rows[0]["name"] == "skill:demo"
    actions = [json.loads(line) for line in (Path(result["run_dir"]) / "actions.jsonl").read_text(encoding="utf-8").splitlines()]
    context_action = next(row for row in actions if row.get("action") == "use_skill_context")
    assert context_action["observation"]["truncated"] is True


@pytest.mark.asyncio
async def test_request_main_tool_enters_waiting_input_and_resumes(tmp_path: Path) -> None:
    runtime = OpenSubagentRuntime(
        settings=Settings(runs_dir=tmp_path / ".runs"),
        llm_client=FakeLLMClient(
            [
                json.dumps(
                    {
                        "action": "request_main_tool",
                        "args": {
                            "reason": "Need current docs",
                            "tool": "web_search",
                            "input": {"query": "Open Subagent MCP"},
                            "expected_output": "links",
                            "sensitivity": "public",
                        },
                    }
                ),
                finish("resumed with broker result"),
            ]
        ),
    )
    response = await runtime.spawn_agent({"agent_type": "explorer", "message": "need web", "cwd": str(tmp_path)})
    agent_id = response["data"]["agent_id"]
    waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 2000})
    first = waited["data"]["completed"][agent_id]
    assert first["status"] == "waiting_input"
    assert first["requested_main_tool"]["tool"] == "web_search"
    assert first["requested_main_tool"]["reason"] == "Need current docs"
    assert first["items_path"] is None
    sent = await runtime.send_input({"target": agent_id, "message": "web result: no links needed"})
    assert sent["ok"]
    waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 2000})
    assert waited["data"]["completed"][agent_id]["status"] == "completed"
