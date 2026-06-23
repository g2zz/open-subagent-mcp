from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_subagent_mcp.actions import ActionExecutor, parse_action
from open_subagent_mcp.locks import RepositoryLockManager
from open_subagent_mcp.models import AgentStatus, AgentType, RunState
from open_subagent_mcp.rollback import rollback_run
from open_subagent_mcp.workspace import utc_now


def state_for(tmp_path: Path, run_dir: Path, segment: str = "seg_0001") -> RunState:
    now = utc_now()
    return RunState(
        agent_id="run_test",
        status=AgentStatus.completed,
        agent_type=AgentType.worker,
        cwd=str(tmp_path),
        run_dir=str(run_dir),
        current_segment_id=segment,
        model="fake",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_rollback_created_file(tmp_path: Path, settings) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = state_for(tmp_path, run_dir)
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    parsed = parse_action(
        json.dumps(
            {
                "action": "write_file",
                "args": {"path": "new.txt", "content": "hello", "mode": "create", "reason": "test"},
            }
        )
    )
    obs = await executor.execute(state, parsed)
    assert obs.ok
    assert (tmp_path / "new.txt").exists()
    result = rollback_run(state=state, segment_id=None, paths=[], include_command_effects=True, force=False)
    assert "new.txt" in result["reverted_files"]
    assert not (tmp_path / "new.txt").exists()


@pytest.mark.asyncio
async def test_segment_rollback_only_reverts_selected_segment(tmp_path: Path, settings) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    state1 = state_for(tmp_path, run_dir, "seg_0001")
    obs1 = await executor.execute(
        state1,
        parse_action(json.dumps({"action": "write_file", "args": {"path": "a.txt", "content": "a", "mode": "create", "reason": "test"}})),
    )
    assert obs1.ok
    state2 = state_for(tmp_path, run_dir, "seg_0002")
    obs2 = await executor.execute(
        state2,
        parse_action(json.dumps({"action": "write_file", "args": {"path": "b.txt", "content": "b", "mode": "create", "reason": "test"}})),
    )
    assert obs2.ok
    result = rollback_run(state=state2, segment_id="seg_0002", paths=[], include_command_effects=True, force=False)
    assert "b.txt" in result["reverted_files"]
    assert (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()


@pytest.mark.asyncio
async def test_rollback_detects_conflict(tmp_path: Path, settings) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = state_for(tmp_path, run_dir)
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    obs = await executor.execute(
        state,
        parse_action(json.dumps({"action": "write_file", "args": {"path": "new.txt", "content": "hello", "mode": "create", "reason": "test"}})),
    )
    assert obs.ok
    (tmp_path / "new.txt").write_text("user change", encoding="utf-8")
    result = rollback_run(state=state, segment_id=None, paths=[], include_command_effects=True, force=False)
    assert "new.txt" in result["conflicted_files"]
    assert (tmp_path / "new.txt").exists()


@pytest.mark.asyncio
async def test_full_run_rollback_restores_original_after_multiple_modifications(tmp_path: Path, settings) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    (tmp_path / "tracked.txt").write_text("A", encoding="utf-8")
    state = state_for(tmp_path, run_dir)
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    for content in ["B", "C"]:
        obs = await executor.execute(
            state,
            parse_action(
                json.dumps(
                    {
                        "action": "write_file",
                        "args": {
                            "path": "tracked.txt",
                            "content": content,
                            "mode": "overwrite",
                            "reason": "test",
                        },
                    }
                )
            ),
        )
        assert obs.ok
    assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "C"
    result = rollback_run(state=state, segment_id=None, paths=[], include_command_effects=True, force=False)
    assert "tracked.txt" in result["reverted_files"]
    assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "A"


@pytest.mark.asyncio
async def test_full_run_rollback_orders_command_and_write_records(tmp_path: Path, settings, python_cmd: str) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    (tmp_path / "tracked.txt").write_text("A", encoding="utf-8")
    state = state_for(tmp_path, run_dir)
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    cmd = f"{python_cmd} -c \"from pathlib import Path; Path('tracked.txt').write_text('B')\""
    command_obs = await executor.execute(
        state,
        parse_action(json.dumps({"action": "run_command", "args": {"cmd": cmd, "reason": "command write"}})),
    )
    assert command_obs.ok
    write_obs = await executor.execute(
        state,
        parse_action(
            json.dumps(
                {
                    "action": "write_file",
                    "args": {
                        "path": "tracked.txt",
                        "content": "C",
                        "mode": "overwrite",
                        "reason": "explicit write",
                    },
                }
            )
        ),
    )
    assert write_obs.ok
    assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "C"
    result = rollback_run(state=state, segment_id=None, paths=[], include_command_effects=True, force=False)
    assert "tracked.txt" in result["reverted_files"]
    assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "A"
