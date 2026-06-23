from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .models import AgentStatus, ErrorCode, FileChange, RunState
from .security import SecurityError, resolve_path
from .workspace import append_jsonl, read_jsonl, remove_empty_parents, snapshot_file, utc_now, write_json


def _current_matches_after(target: Path, change: FileChange) -> bool:
    current = snapshot_file(target, rel_path=change.path)
    after = change.after
    if current.exists != after.exists:
        return False
    if not current.exists and not after.exists:
        return True
    if current.type != after.type:
        return False
    if current.sha256 != after.sha256:
        return False
    if current.mode != after.mode:
        return False
    return True


def _remove_path(target: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)
    elif target.is_dir():
        try:
            target.rmdir()
        except OSError:
            pass


def _restore_before(target: Path, change: FileChange) -> None:
    before = change.before
    if not before.exists:
        _remove_path(target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if before.type == "dir":
        target.mkdir(parents=True, exist_ok=True)
        if before.mode is not None:
            os.chmod(target, before.mode)
        return
    if before.type == "symlink":
        _remove_path(target)
        if before.link_target is not None:
            os.symlink(before.link_target, target)
        return
    if before.type == "file":
        if before.content_path is None:
            raise OSError(f"missing before snapshot for {change.path}")
        target.write_bytes(Path(before.content_path).read_bytes())
        if before.mode is not None:
            os.chmod(target, before.mode)


def load_changes(run_dir: Path, *, include_command_effects: bool) -> list[FileChange]:
    rows = read_jsonl(run_dir / "writes.jsonl")
    if include_command_effects:
        rows.extend(read_jsonl(run_dir / "command_effects.jsonl"))
    changes = [FileChange.model_validate(row) for row in rows]
    return sorted(changes, key=lambda change: change.timestamp)


def collapse_changes(changes: list[FileChange]) -> list[FileChange]:
    grouped: dict[str, list[FileChange]] = {}
    for change in changes:
        grouped.setdefault(change.path, []).append(change)
    collapsed: list[FileChange] = []
    for group in grouped.values():
        first = group[0]
        last = group[-1]
        merged = last.model_copy(deep=True)
        merged.before = first.before
        merged.effect = first.effect
        collapsed.append(merged)
    last_index = {change.path: index for index, change in enumerate(changes)}
    return sorted(collapsed, key=lambda change: last_index[change.path])


def rollback_run(
    *,
    state: RunState,
    segment_id: str | None,
    paths: Iterable[str],
    include_command_effects: bool,
    force: bool,
) -> dict:
    run_dir = Path(state.run_dir)
    cwd = Path(state.cwd).resolve()
    wanted_paths = {p for p in paths if p}
    changes = load_changes(run_dir, include_command_effects=include_command_effects)
    if segment_id:
        changes = [c for c in changes if c.segment_id == segment_id]
    if wanted_paths:
        changes = [c for c in changes if c.path in wanted_paths]
    changes = collapse_changes(changes)

    reverted: list[str] = []
    conflicts: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []

    for change in reversed(changes):
        try:
            target = resolve_path(
                change.path,
                cwd,
                [],
                operation="write",
                explicit_authorizations=state.explicit_authorizations,
            )
            if not force and not _current_matches_after(target, change):
                conflicts.append(change.path)
                continue
            _restore_before(target, change)
            if not change.before.exists:
                remove_empty_parents(target, cwd)
            reverted.append(change.path)
        except SecurityError as exc:
            errors.append({"path": change.path, "code": exc.error.code.value, "message": exc.error.message})
        except Exception as exc:
            errors.append({"path": change.path, "code": ErrorCode.io_error.value, "message": str(exc)})

    status = AgentStatus.rolled_back if not segment_id and not conflicts and not errors else AgentStatus.partially_rolled_back
    result = {
        "agent_id": state.agent_id,
        "segment_id": segment_id,
        "status": status.value,
        "reverted_files": sorted(set(reverted)),
        "conflicted_files": sorted(set(conflicts)),
        "skipped_files": sorted(set(skipped)),
        "errors": errors,
    }
    append_jsonl(run_dir / "rollback.jsonl", {"timestamp": utc_now(), **result})
    state.status = status
    state.updated_at = utc_now()
    write_json(run_dir / "state.json", state.model_dump(mode="json"))
    return result
