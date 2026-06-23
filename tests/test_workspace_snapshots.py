from __future__ import annotations

import os
from pathlib import Path

from open_subagent_mcp.workspace import diff_snapshots, scan_workspace


def test_snapshot_detects_create_modify_delete_and_mode(tmp_path: Path) -> None:
    run_dir = tmp_path / ".runs" / "run_test"
    file = tmp_path / "a.txt"
    file.write_text("one", encoding="utf-8")
    delete_me = tmp_path / "delete.txt"
    delete_me.write_text("gone", encoding="utf-8")
    mode_me = tmp_path / "mode.sh"
    mode_me.write_text("echo hi", encoding="utf-8")
    before = scan_workspace(tmp_path, run_dir=run_dir, snapshot_name="before")

    file.write_text("two", encoding="utf-8")
    delete_me.unlink()
    (tmp_path / "new.txt").write_text("new", encoding="utf-8")
    os.chmod(mode_me, 0o755)
    after = scan_workspace(tmp_path, run_dir=run_dir, snapshot_name="after")

    changes = diff_snapshots(
        before,
        after,
        agent_id="run_test",
        segment_id="seg_0001",
        action_id="act_1",
        source="command",
    )
    effects = {c.path: c.effect for c in changes}
    assert effects["a.txt"] == "modified"
    assert effects["delete.txt"] == "deleted"
    assert effects["new.txt"] == "created"
    assert effects["mode.sh"] == "mode_changed"
