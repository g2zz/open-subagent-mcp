from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from open_subagent_mcp.actions import ActionExecutor, parse_action
from open_subagent_mcp.locks import RepositoryLockManager
from open_subagent_mcp.models import AgentStatus, AgentType, RunState
from open_subagent_mcp.workspace import utc_now


def state_for(tmp_path: Path, run_dir: Path) -> RunState:
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
async def test_apply_patch_validates_deleted_sensitive_path(tmp_path: Path, settings) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    (tmp_path / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    patch = """diff --git a/.env b/.env
deleted file mode 100644
--- a/.env
+++ /dev/null
@@ -1 +0,0 @@
-TOKEN=x
"""
    obs = await executor.execute(
        state_for(tmp_path, run_dir),
        parse_action(json.dumps({"action": "apply_patch", "args": {"patch": patch, "reason": "delete"}})),
    )
    assert not obs.ok
    assert obs.error is not None
    assert obs.error.code.value == "sensitive_path_blocked"
    assert (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_apply_patch_strips_timestamp_before_security_check(tmp_path: Path, settings) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)
    (tmp_path / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    patch = """--- a/.env\t2026-06-17 00:00:00
+++ b/.env\t2026-06-17 00:00:01
@@ -1 +1 @@
-TOKEN=x
+TOKEN=y
"""
    obs = await executor.execute(
        state_for(tmp_path, run_dir),
        parse_action(json.dumps({"action": "apply_patch", "args": {"patch": patch, "reason": "edit"}})),
    )
    assert not obs.ok
    assert obs.error is not None
    assert obs.error.code.value == "sensitive_path_blocked"
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "TOKEN=x\n"


@pytest.mark.asyncio
async def test_search_fallback_skips_symlink_escape(tmp_path: Path, settings, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path.parent / "outside-search.txt"
    outside.write_text("needle outside", encoding="utf-8")
    (tmp_path / "safe.txt").write_text("needle inside", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    run_dir = tmp_path / ".runs" / "run_test"
    run_dir.mkdir(parents=True)

    async def missing_rg(*args, **kwargs):
        raise FileNotFoundError("rg")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_rg)
    executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
    obs = await executor.execute(
        state_for(tmp_path, run_dir),
        parse_action(json.dumps({"action": "search", "args": {"query": "needle", "path": "."}})),
    )
    assert obs.ok
    matches = obs.data["matches"] if obs.data else []
    assert any("safe.txt" in match for match in matches)
    assert not any("outside-search.txt" in match or "link.txt" in match for match in matches)
