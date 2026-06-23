from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_subagent_mcp.actions import ActionExecutor, parse_action
from open_subagent_mcp.locks import RepositoryLockManager
from open_subagent_mcp.models import AgentStatus, AgentType, RunState
from open_subagent_mcp.workspace import read_jsonl, utc_now


def make_state(tmp_path: Path, run_dir: Path) -> RunState:
    now = utc_now()
    return RunState(
        agent_id="run_test",
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
async def test_run_command_records_file_effects(tmp_path: Path, settings, python_cmd: str) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = make_state(tmp_path, run_dir)
    executor = ActionExecutor(
        settings=settings,
        lock_manager=RepositoryLockManager(),
        active_processes={},
    )
    cmd = f"{python_cmd} -c \"from pathlib import Path; Path('made.txt').write_text('x')\""
    parsed = parse_action(json.dumps({"action": "run_command", "args": {"cmd": cmd, "reason": "test"}}))
    obs = await executor.execute(state, parsed)
    assert obs.ok
    effects = read_jsonl(run_dir / "command_effects.jsonl")
    assert any(row["path"] == "made.txt" and row["effect"] == "created" for row in effects)
    commands = read_jsonl(run_dir / "commands.jsonl")
    assert commands[0]["stdout_path"]


@pytest.mark.asyncio
async def test_explorer_rejects_write_command(tmp_path: Path, settings, python_cmd: str) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = make_state(tmp_path, run_dir)
    state.agent_type = AgentType.explorer
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    cmd = f"{python_cmd} -c \"open('x.txt','w').write('x')\""
    parsed = parse_action(json.dumps({"action": "run_command", "args": {"cmd": cmd, "reason": "test"}}))
    obs = await executor.execute(state, parsed)
    assert not obs.ok


@pytest.mark.asyncio
async def test_run_command_blocks_external_write_path(tmp_path: Path, settings, python_cmd: str) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = make_state(tmp_path, run_dir)
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    outside = tmp_path.parent / "outside-command-write.txt"
    cmd = f"{python_cmd} -c \"from pathlib import Path; Path('{outside}').write_text('x')\""
    parsed = parse_action(json.dumps({"action": "run_command", "args": {"cmd": cmd, "reason": "block outside"}}))
    obs = await executor.execute(state, parsed)
    assert not obs.ok
    assert obs.error is not None
    assert obs.error.code.value == "path_escape_blocked"
    assert not outside.exists()


@pytest.mark.asyncio
async def test_run_command_blocks_sensitive_path(tmp_path: Path, settings) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")
    state = make_state(tmp_path, run_dir)
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    parsed = parse_action(json.dumps({"action": "run_command", "args": {"cmd": "cat .env", "reason": "read", "read_only": True}}))
    obs = await executor.execute(state, parsed)
    assert not obs.ok
    assert obs.error is not None
    assert obs.error.code.value == "sensitive_path_blocked"


@pytest.mark.asyncio
async def test_run_command_blocks_bare_symlink_escape_read(tmp_path: Path, settings) -> None:
    outside = tmp_path.parent / "outside-command-read.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "link").symlink_to(outside)
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = make_state(tmp_path, run_dir)
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    parsed = parse_action(
        json.dumps(
            {
                "action": "run_command",
                "args": {"cmd": "cat link", "reason": "read symlink", "read_only": True},
            }
        )
    )
    obs = await executor.execute(state, parsed)
    assert not obs.ok
    assert obs.error is not None
    assert obs.error.code.value == "path_escape_blocked"


@pytest.mark.asyncio
async def test_run_command_blocks_redirect_symlink_escape_read(tmp_path: Path, settings) -> None:
    outside = tmp_path.parent / "outside-redirect-read.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "link").symlink_to(outside)
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = make_state(tmp_path, run_dir)
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    parsed = parse_action(
        json.dumps(
            {
                "action": "run_command",
                "args": {"cmd": "cat<link", "reason": "read symlink", "read_only": True},
            }
        )
    )
    obs = await executor.execute(state, parsed)
    assert not obs.ok
    assert obs.error is not None
    assert obs.error.code.value == "path_escape_blocked"


@pytest.mark.asyncio
async def test_run_command_blocks_redirect_sensitive_write(tmp_path: Path, settings) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    state = make_state(tmp_path, run_dir)
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    parsed = parse_action(
        json.dumps(
            {
                "action": "run_command",
                "args": {"cmd": "echo x >id_rsa", "reason": "write sensitive"},
            }
        )
    )
    obs = await executor.execute(state, parsed)
    assert not obs.ok
    assert obs.error is not None
    assert obs.error.code.value == "sensitive_path_blocked"
    assert not (tmp_path / "id_rsa").exists()
