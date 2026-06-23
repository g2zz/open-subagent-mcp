from __future__ import annotations

from pathlib import Path

import pytest

from open_subagent_mcp.models import Authorization, ErrorCode
from open_subagent_mcp.security import SecurityError, resolve_path, sanitize_environment


def test_blocks_dotdot_escape(tmp_path: Path) -> None:
    with pytest.raises(SecurityError) as exc:
        resolve_path("../outside.txt", tmp_path, [], operation="read", explicit_authorizations=[])
    assert exc.value.error.code == ErrorCode.path_escape_blocked


def test_blocks_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(SecurityError) as exc:
        resolve_path("link", tmp_path, [], operation="read", explicit_authorizations=[])
    assert exc.value.error.code == ErrorCode.path_escape_blocked


def test_blocks_writing_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(SecurityError) as exc:
        resolve_path("link", tmp_path, [], operation="write", explicit_authorizations=[])
    assert exc.value.error.code == ErrorCode.path_escape_blocked


def test_blocks_sensitive_paths(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=x", encoding="utf-8")
    with pytest.raises(SecurityError) as exc:
        resolve_path(".env", tmp_path, [], operation="read", explicit_authorizations=[])
    assert exc.value.error.code == ErrorCode.sensitive_path_blocked


def test_external_root_requires_authorization(tmp_path: Path) -> None:
    outside_root = tmp_path.parent / "allowed-root"
    outside_root.mkdir(exist_ok=True)
    file = outside_root / "note.txt"
    file.write_text("ok", encoding="utf-8")
    with pytest.raises(SecurityError):
        resolve_path(str(file), tmp_path, [outside_root], operation="read", explicit_authorizations=[])
    resolved = resolve_path(
        str(file),
        tmp_path,
        [outside_root],
        operation="read",
        explicit_authorizations=[Authorization.read_external_roots],
    )
    assert resolved == file.resolve()


def test_sensitive_environment_is_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "hidden")
    monkeypatch.setenv("PATH", "/bin")
    env = sanitize_environment()
    assert "API_KEY" not in env
    assert env["PATH"] == "/bin"
