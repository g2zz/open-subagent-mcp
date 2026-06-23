from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from open_subagent_mcp.actions import ActionExecutor, parse_action
from open_subagent_mcp.config import Settings
from open_subagent_mcp.llm_client import FakeLLMClient
from open_subagent_mcp.locks import RepositoryLockManager
from open_subagent_mcp.models import AgentStatus, AgentType, RunState
from open_subagent_mcp.rollback import rollback_run
from open_subagent_mcp.runtime import OpenSubagentRuntime
from open_subagent_mcp.workspace import utc_now


def finish(summary: str = "done") -> str:
    return json.dumps(
        {
            "action": "finish",
            "args": {
                "status": "completed",
                "summary": summary,
                "self_check_commands": ["manual"],
                "tests": [],
                "risk_notes": ["checked in fake runtime"],
                "open_issues": [],
            },
        }
    )


def run_state(tmp_path: Path, run_dir: Path, agent_id: str) -> RunState:
    now = utc_now()
    return RunState(
        agent_id=agent_id,
        status=AgentStatus.running,
        agent_type=AgentType.worker,
        cwd=str(tmp_path),
        run_dir=str(run_dir),
        current_segment_id="seg_0001",
        model="fake",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_json_schema_error_recovery_and_fake_messages(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    fake = FakeLLMClient(
        [
            "not json",
            json.dumps({"action": "read_file", "args": {"path": "note.txt"}}),
            finish(),
        ]
    )
    runtime = OpenSubagentRuntime(settings=Settings(runs_dir=tmp_path / ".runs"), llm_client=fake)
    spawned = await runtime.spawn_agent({"agent_type": "explorer", "message": "read note", "cwd": str(tmp_path)})
    agent_id = spawned["data"]["agent_id"]
    waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 3000})
    assert waited["data"]["completed"][agent_id]["status"] == "completed"
    serialized_requests = json.dumps(fake.requests, ensure_ascii=False)
    assert "任务包" in serialized_requests
    assert "observation" in serialized_requests
    assert "seg_0001" in serialized_requests
    assert "无法解析" in serialized_requests


@pytest.mark.asyncio
async def test_command_effect_rollback(tmp_path: Path, settings, python_cmd: str) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = run_state(tmp_path, run_dir, "run_test")
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    cmd = f"{python_cmd} -c \"from pathlib import Path; Path('cmd.txt').write_text('x')\""
    obs = await executor.execute(
        state,
        parse_action(json.dumps({"action": "run_command", "args": {"cmd": cmd, "reason": "create"}})),
    )
    assert obs.ok
    assert (tmp_path / "cmd.txt").exists()
    result = rollback_run(state=state, segment_id=None, paths=[], include_command_effects=True, force=False)
    assert "cmd.txt" in result["reverted_files"]
    assert not (tmp_path / "cmd.txt").exists()


@pytest.mark.asyncio
async def test_run_command_write_lock_serializes_same_repo(tmp_path: Path, settings, python_cmd: str) -> None:
    lock_manager = RepositoryLockManager()
    executor = ActionExecutor(settings=settings, lock_manager=lock_manager, active_processes={})
    run_a = tmp_path / ".runs" / "run_a"
    run_b = tmp_path / ".runs" / "run_b"
    run_a.mkdir(parents=True)
    run_b.mkdir(parents=True)
    state_a = run_state(tmp_path, run_a, "run_a")
    state_b = run_state(tmp_path, run_b, "run_b")
    cmd = f"{python_cmd} -c \"import time; time.sleep(0.2); open('lock.log','a').write('x')\""
    parsed_a = parse_action(json.dumps({"action": "run_command", "args": {"cmd": cmd, "reason": "lock"}}))
    parsed_b = parse_action(json.dumps({"action": "run_command", "args": {"cmd": cmd, "reason": "lock"}}))
    start = time.monotonic()
    await asyncio.gather(executor.execute(state_a, parsed_a), executor.execute(state_b, parsed_b))
    elapsed = time.monotonic() - start
    assert elapsed >= 0.35
    assert (tmp_path / "lock.log").read_text(encoding="utf-8") == "xx"


@pytest.mark.asyncio
async def test_close_terminates_running_process(tmp_path: Path, python_cmd: str) -> None:
    slow = json.dumps(
        {
            "action": "run_command",
            "args": {"cmd": f"{python_cmd} -c \"import time; time.sleep(10)\"", "reason": "sleep"},
        }
    )
    runtime = OpenSubagentRuntime(
        settings=Settings(runs_dir=tmp_path / ".runs"),
        llm_client=FakeLLMClient([slow]),
    )
    spawned = await runtime.spawn_agent({"agent_type": "worker", "message": "sleep", "cwd": str(tmp_path)})
    agent_id = spawned["data"]["agent_id"]
    await asyncio.sleep(0.2)
    closed = await runtime.close_agent({"target": agent_id})
    assert closed["data"]["status"] == "closed"
    state = runtime.store.load_state(agent_id)
    assert state.status == AgentStatus.closed
