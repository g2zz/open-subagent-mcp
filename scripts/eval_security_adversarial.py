from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from open_subagent_mcp.actions import ActionExecutor, parse_action
from open_subagent_mcp.config import Settings
from open_subagent_mcp.locks import RepositoryLockManager
from open_subagent_mcp.models import AgentStatus, AgentType, RunState
from open_subagent_mcp.rollback import rollback_run
from open_subagent_mcp.workspace import utc_now


def state_for(workspace: Path, run_dir: Path, agent_id: str = "run_eval", segment: str = "seg_0001") -> RunState:
    now = utc_now()
    return RunState(
        agent_id=agent_id,
        status=AgentStatus.running,
        agent_type=AgentType.worker,
        cwd=str(workspace),
        run_dir=str(run_dir),
        current_segment_id=segment,
        model="fake",
        created_at=now,
        updated_at=now,
    )


def action(name: str, args: dict) -> str:
    return json.dumps({"action": name, "args": args})


async def main() -> None:
    eval_root = Path(tempfile.mkdtemp(prefix="open-subagent-mcp-security-eval-"))
    try:
        workspace = eval_root / "workspace"
        run_dir = eval_root / ".runs" / "run_eval"
        workspace.mkdir()
        run_dir.mkdir(parents=True)
        settings = Settings(runs_dir=eval_root / ".runs")
        executor = ActionExecutor(settings=settings, lock_manager=RepositoryLockManager(), active_processes={})
        results: list[str] = []

        outside = eval_root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        (workspace / "link").symlink_to(outside)
        obs = await executor.execute(
            state_for(workspace, run_dir),
            parse_action(action("run_command", {"cmd": "cat<link", "reason": "redirect symlink read", "read_only": True})),
        )
        assert not obs.ok and obs.error and obs.error.code.value == "path_escape_blocked"
        results.append("redirect_symlink_read_blocked")

        obs = await executor.execute(
            state_for(workspace, run_dir),
            parse_action(action("run_command", {"cmd": "echo x >id_rsa", "reason": "redirect sensitive write"})),
        )
        assert not obs.ok and obs.error and obs.error.code.value == "sensitive_path_blocked"
        assert not (workspace / "id_rsa").exists()
        results.append("redirect_sensitive_write_blocked")
        (workspace / "link").unlink()

        patch = """--- a/.env\t2026-06-17 00:00:00
+++ b/.env\t2026-06-17 00:00:01
@@ -1 +1 @@
-TOKEN=x
+TOKEN=y
"""
        (workspace / ".env").write_text("TOKEN=x\n", encoding="utf-8")
        obs = await executor.execute(
            state_for(workspace, run_dir),
            parse_action(action("apply_patch", {"patch": patch, "reason": "edit sensitive"})),
        )
        assert not obs.ok and obs.error and obs.error.code.value == "sensitive_path_blocked"
        assert (workspace / ".env").read_text(encoding="utf-8") == "TOKEN=x\n"
        results.append("patch_sensitive_timestamp_blocked")

        tracked = workspace / "tracked.txt"
        tracked.write_text("A", encoding="utf-8")
        python_cmd = (
            f"{sys.executable} -c \"from pathlib import Path; "
            "Path('tracked.txt').write_text('B')\""
        )
        obs = await executor.execute(
            state_for(workspace, run_dir),
            parse_action(action("run_command", {"cmd": python_cmd, "reason": "command write"})),
        )
        assert obs.ok
        obs = await executor.execute(
            state_for(workspace, run_dir),
            parse_action(action("write_file", {"path": "tracked.txt", "content": "C", "mode": "overwrite", "reason": "explicit write"})),
        )
        assert obs.ok
        result = rollback_run(
            state=state_for(workspace, run_dir),
            segment_id=None,
            paths=[],
            include_command_effects=True,
            force=False,
        )
        assert "tracked.txt" in result["reverted_files"]
        assert tracked.read_text(encoding="utf-8") == "A"
        results.append("rollback_cross_source_restored_original")

        print(json.dumps({"ok": True, "eval": "security_adversarial", "results": results}, ensure_ascii=False))
        shutil.rmtree(eval_root)
    except Exception as exc:
        print(json.dumps({"ok": False, "eval": "security_adversarial", "archive_dir": str(eval_root), "error": str(exc)}, ensure_ascii=False))
        raise


if __name__ == "__main__":
    asyncio.run(main())
