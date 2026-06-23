from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import FileChange, FileSnapshot


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_file(path: Path, *, rel_path: str, content_dir: Path | None = None) -> FileSnapshot:
    if not path.exists() and not path.is_symlink():
        return FileSnapshot(path=rel_path, type="missing", exists=False)
    stat = path.lstat()
    if path.is_symlink():
        return FileSnapshot(
            path=rel_path,
            type="symlink",
            exists=True,
            size=stat.st_size,
            mode=stat.st_mode & 0o7777,
            mtime=stat.st_mtime,
            link_target=os.readlink(path),
        )
    if path.is_dir():
        return FileSnapshot(
            path=rel_path,
            type="dir",
            exists=True,
            size=None,
            mode=stat.st_mode & 0o7777,
            mtime=stat.st_mtime,
        )
    content_path = None
    file_hash = sha256_path(path)
    if content_dir is not None:
        content_dir.mkdir(parents=True, exist_ok=True)
        safe_name = hashlib.sha256(rel_path.encode("utf-8")).hexdigest()
        content_path = content_dir / safe_name
        shutil.copy2(path, content_path)
    return FileSnapshot(
        path=rel_path,
        type="file",
        exists=True,
        size=stat.st_size,
        mode=stat.st_mode & 0o7777,
        sha256=file_hash,
        mtime=stat.st_mtime,
        content_path=str(content_path) if content_path else None,
    )


def _is_ignored_dir(path: Path, ignore_dirs: set[str]) -> bool:
    return path.name in ignore_dirs


def scan_workspace(
    root: Path,
    *,
    run_dir: Path | None = None,
    snapshot_name: str | None = None,
    ignore_dirs: Iterable[str] = (),
) -> dict[str, FileSnapshot]:
    root = root.resolve()
    ignore = set(ignore_dirs)
    content_dir = None
    if run_dir and snapshot_name:
        content_dir = run_dir / "snapshots" / snapshot_name
    result: dict[str, FileSnapshot] = {}
    if not root.exists():
        return result
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        rel_dir = "." if current_path == root else current_path.relative_to(root).as_posix()
        kept_dirs = []
        for dirname in dirnames:
            child = current_path / dirname
            child_rel = child.relative_to(root).as_posix()
            if _is_ignored_dir(child, ignore):
                result[child_rel] = snapshot_file(child, rel_path=child_rel)
            else:
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        if rel_dir != ".":
            result[rel_dir] = snapshot_file(current_path, rel_path=rel_dir)
        for filename in filenames:
            child = current_path / filename
            rel = child.relative_to(root).as_posix()
            result[rel] = snapshot_file(child, rel_path=rel, content_dir=content_dir)
    return result


def detect_symlink_escapes(root: Path, *, ignore_dirs: Iterable[str] = ()) -> list[str]:
    root = root.resolve()
    ignore = set(ignore_dirs)
    escapes: list[str] = []
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        kept_dirs = []
        for dirname in dirnames:
            child = current_path / dirname
            if child.name in ignore:
                continue
            if child.is_symlink():
                target = child.resolve(strict=False)
                try:
                    target.relative_to(root)
                except ValueError:
                    escapes.append(child.relative_to(root).as_posix())
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in filenames:
            child = current_path / filename
            if child.is_symlink():
                target = child.resolve(strict=False)
                try:
                    target.relative_to(root)
                except ValueError:
                    escapes.append(child.relative_to(root).as_posix())
    return sorted(set(escapes))


def classify_change(before: FileSnapshot, after: FileSnapshot) -> str | None:
    if not before.exists and after.exists:
        return "created"
    if before.exists and not after.exists:
        return "deleted"
    if before.exists and after.exists and before.type != after.type:
        return "type_changed"
    if before.exists and after.exists and before.mode != after.mode:
        if before.sha256 == after.sha256 and before.type == after.type:
            return "mode_changed"
        return "modified"
    if before.exists and after.exists and before.sha256 != after.sha256:
        return "modified"
    return None


def diff_snapshots(
    before: dict[str, FileSnapshot],
    after: dict[str, FileSnapshot],
    *,
    agent_id: str,
    segment_id: str,
    action_id: str,
    source: str,
) -> list[FileChange]:
    changes: list[FileChange] = []
    timestamp = utc_now()
    for rel in sorted(set(before) | set(after)):
        before_entry = before.get(rel) or FileSnapshot(path=rel, type="missing", exists=False)
        after_entry = after.get(rel) or FileSnapshot(path=rel, type="missing", exists=False)
        effect = classify_change(before_entry, after_entry)
        if effect is None:
            continue
        changes.append(
            FileChange(
                agent_id=agent_id,
                segment_id=segment_id,
                action_id=action_id,
                path=rel,
                effect=effect,  # type: ignore[arg-type]
                before=before_entry,
                after=after_entry,
                timestamp=timestamp,
                source=source,  # type: ignore[arg-type]
            )
        )
    return changes


def append_jsonl(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def git_status(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return ""
    return result.stdout


def git_diff(root: Path, paths: list[str] | None = None) -> str:
    args = ["git", "diff", "--"]
    args.extend(paths or [])
    try:
        result = subprocess.run(
            args,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return ""


def text_diff(path: str, before: bytes | None, after: bytes | None) -> str:
    before_text = (before or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
    after_text = (after or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(before_text, after_text, fromfile=f"a/{path}", tofile=f"b/{path}")
    )


def non_git_diff_from_changes(changes: list[FileChange]) -> str:
    chunks: list[str] = []
    for change in changes:
        before_bytes = None
        after_bytes = None
        if change.before.content_path:
            before_bytes = Path(change.before.content_path).read_bytes()
        if change.after.content_path:
            after_bytes = Path(change.after.content_path).read_bytes()
        chunks.append(text_diff(change.path, before_bytes, after_bytes))
    return "\n".join(chunks)


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n[truncated]", True


def remove_empty_parents(path: Path, root: Path) -> None:
    current = path.parent
    root = root.resolve()
    while current != root and root in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def action_id(prefix: str = "act") -> str:
    return f"{prefix}_{time.time_ns()}"
