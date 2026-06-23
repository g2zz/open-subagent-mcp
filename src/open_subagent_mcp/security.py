from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from .models import Authorization, ErrorCode, ToolError

SENSITIVE_NAMES = {
    "id_rsa",
    "id_ed25519",
}

SENSITIVE_PARTS = {
    ".ssh",
    ".aws",
}

SENSITIVE_SUFFIXES = {
    ".pem",
    ".key",
}


class SecurityError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.error = ToolError(code=code, message=message, details=details or {})


def is_relative_escape(raw_path: str) -> bool:
    parts = Path(raw_path).parts
    return ".." in parts


def is_sensitive_path(path: Path, extra_patterns: list[str] | None = None) -> bool:
    lowered = str(path).lower()
    name = path.name.lower()
    parts = {p.lower() for p in path.parts}
    if name == ".env" or name.startswith(".env."):
        return True
    if name in SENSITIVE_NAMES:
        return True
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        return True
    if SENSITIVE_PARTS & parts:
        return True
    if ".config" in parts and "gcloud" in parts:
        return True
    if any(term in lowered for term in ("secret", "credential", "token")):
        return True
    for pattern in extra_patterns or []:
        if re.search(pattern, str(path)):
            return True
    return False


def _real(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def ensure_existing_dir(path: str, field: str) -> Path:
    real = _real(Path(path))
    if not real.exists() or not real.is_dir():
        raise SecurityError(ErrorCode.invalid_request, f"{field} must be an existing directory")
    return real


def normalize_roots(cwd: str, allowed_external_roots: list[str]) -> tuple[Path, list[Path]]:
    root = ensure_existing_dir(cwd, "cwd")
    external: list[Path] = []
    for item in allowed_external_roots:
        external.append(ensure_existing_dir(item, "allowed_external_roots"))
    return root, external


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_path(
    raw_path: str,
    cwd: Path,
    allowed_external_roots: list[Path],
    *,
    operation: str,
    explicit_authorizations: list[Authorization],
    sensitive_patterns: list[str] | None = None,
) -> Path:
    if not raw_path:
        raise SecurityError(ErrorCode.invalid_request, "path is required")
    if is_relative_escape(raw_path):
        raise SecurityError(ErrorCode.path_escape_blocked, "path contains '..'", {"path": raw_path})
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    if operation == "write" and candidate.is_symlink():
        raise SecurityError(ErrorCode.path_escape_blocked, "writing symlink paths is blocked")
    real = _real(candidate)
    allowed_roots = [cwd]
    if operation == "read":
        if allowed_external_roots and Authorization.read_external_roots in explicit_authorizations:
            allowed_roots.extend(allowed_external_roots)
    if not any(is_within(real, root) or real == root for root in allowed_roots):
        raise SecurityError(
            ErrorCode.path_escape_blocked,
            "path is outside allowed roots",
            {"path": str(real), "roots": [str(r) for r in allowed_roots]},
        )
    if is_sensitive_path(real, sensitive_patterns):
        raise SecurityError(ErrorCode.sensitive_path_blocked, "sensitive path is blocked", {"path": str(real)})
    return real


def sanitize_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    source = env or os.environ
    safe: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if any(term in upper for term in ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")):
            continue
        if upper in {"PATH", "HOME", "SHELL", "LANG"} or upper.startswith("LC_"):
            safe[key] = value
    return safe


def redact_text(text: str) -> str:
    text = re.sub(r"(?i)(api[_-]?key|token|secret|password|credential)(['\"]?\s*[:=]\s*['\"]?)[^'\"\s]+", r"\1\2[REDACTED]", text)
    text = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-[REDACTED]", text)
    return text


def command_risks(cmd: str) -> set[str]:
    risks: set[str] = set()
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    lowered = [t.lower() for t in tokens]
    joined = " ".join(lowered)
    if any(op in cmd for op in (">", ">>", "2>", "| tee")):
        risks.add("writes")
    if re.search(r"\b(write_text|write_bytes|open\s*\([^)]*['\"]w|mkdir|touch)\b", cmd):
        risks.add("writes")
    if any(t in lowered for t in ("rm", "mv", "cp", "chmod", "chown", "truncate")):
        risks.add("destructive")
    if "rm -rf" in joined or "sudo " in f" {joined} ":
        risks.add("dangerous")
    if any(t in lowered for t in ("pip", "npm", "pnpm", "yarn", "uv", "brew")) and any(
        t in lowered for t in ("install", "add", "sync")
    ):
        risks.add("install_dependencies")
    if lowered[:2] == ["git", "commit"]:
        risks.add("git_commit")
    if lowered[:2] == ["git", "push"] or " remote " in joined:
        risks.add("git_push")
    if any(path in joined for path in ("/etc", "~/.ssh", ".ssh/", ".aws/", ".config/gcloud")):
        risks.add("system_config")
    if any(term in joined for term in ("prod", "production", "kubectl", "terraform apply")):
        risks.add("production_operations")
    return risks


def _strip_shell_path(raw: str) -> str:
    cleaned = raw.strip().strip("'\"").strip(";,)")
    cleaned = re.sub(r"^(?:\d?>{1,2}|\d?<|>{1,2}|<)", "", cleaned)
    return cleaned.strip().strip("'\"").strip(";,)")


def _looks_like_path(raw: str) -> bool:
    if not raw or "://" in raw:
        return False
    if raw.startswith("-"):
        return False
    if raw.startswith(("/", "~", "./", "../", ".env")):
        return True
    if "/" in raw:
        return True
    lowered = raw.lower()
    if lowered in SENSITIVE_NAMES or any(part in lowered for part in ("secret", "credential", "token")):
        return True
    if re.match(r"^[A-Za-z0-9_.-]+$", raw) and "." in raw:
        return True
    return False


def extract_command_paths(cmd: str) -> list[str]:
    paths: list[str] = []
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    for index, token in enumerate(tokens):
        cleaned = _strip_shell_path(token)
        if index == 0:
            continue
        if "=" in cleaned and not cleaned.startswith("="):
            cleaned = cleaned.split("=", 1)[1]
        if _looks_like_path(cleaned):
            paths.append(cleaned)
    for match in re.finditer(
        r"(?P<path>/(?:[^'\"\s`;|)]+)|(?:~|\.\.?/)[^'\"\s`;|)]+|\.env(?:\.[A-Za-z0-9_.-]+)?)",
        cmd,
    ):
        raw = _strip_shell_path(match.group("path"))
        if _looks_like_path(raw):
            paths.append(raw)
    for match in re.finditer(r"(?:\d?>{1,2}|\d?<|>{1,2}|<)\s*(?P<path>[A-Za-z0-9_.-]+)", cmd):
        raw = _strip_shell_path(match.group("path"))
        if raw:
            paths.append(raw)
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            unique.append(path)
            seen.add(path)
    return unique


def validate_command_paths(
    cmd: str,
    *,
    cwd: Path,
    allowed_external_roots: list[Path],
    operation: str,
    explicit_authorizations: list[Authorization],
    sensitive_patterns: list[str] | None = None,
) -> None:
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    ignored_executable: Path | None = None
    if tokens and _looks_like_path(tokens[0]):
        ignored_executable = Path(tokens[0]).expanduser().resolve(strict=False)
    for raw_path in extract_command_paths(cmd):
        candidate = Path(raw_path)
        if ignored_executable is not None:
            resolved = (
                candidate.expanduser().resolve(strict=False)
                if candidate.is_absolute()
                else (cwd / candidate).resolve(strict=False)
            )
            if resolved == ignored_executable:
                continue
        resolve_path(
            raw_path,
            cwd,
            allowed_external_roots,
            operation=operation,
            explicit_authorizations=explicit_authorizations,
            sensitive_patterns=sensitive_patterns,
        )
    for index, token in enumerate(tokens):
        if index == 0:
            continue
        cleaned = _strip_shell_path(token)
        if not cleaned or cleaned.startswith("-"):
            continue
        candidate = cwd / cleaned
        if candidate.exists() or candidate.is_symlink():
            resolve_path(
                cleaned,
                cwd,
                allowed_external_roots,
                operation=operation,
                explicit_authorizations=explicit_authorizations,
                sensitive_patterns=sensitive_patterns,
            )


def authorize_command(
    cmd: str,
    *,
    read_only: bool,
    timeout_seconds: int,
    default_timeout_seconds: int,
    explicit_authorizations: list[Authorization],
) -> tuple[bool, set[str]]:
    risks = command_risks(cmd)
    if "dangerous" in risks and Authorization.destructive_actions not in explicit_authorizations:
        raise SecurityError(ErrorCode.dangerous_command_blocked, "dangerous command requires authorization", {"risks": sorted(risks)})
    auth_map = {
        "destructive": Authorization.destructive_actions,
        "install_dependencies": Authorization.install_dependencies,
        "git_commit": Authorization.git_commit,
        "git_push": Authorization.git_push,
        "system_config": Authorization.system_config,
        "production_operations": Authorization.production_operations,
    }
    for risk, auth in auth_map.items():
        if risk in risks and auth not in explicit_authorizations:
            raise SecurityError(ErrorCode.authorization_required, f"{risk} requires {auth.value}", {"risk": risk})
    if timeout_seconds > default_timeout_seconds and Authorization.long_running_commands not in explicit_authorizations:
        raise SecurityError(ErrorCode.authorization_required, "long command requires authorization")
    effective_read_only = read_only and not (risks & {"writes", "destructive", "install_dependencies"})
    return effective_read_only, risks
