from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import Settings
from .locks import RepositoryLockManager
from .models import (
    AgentType,
    ErrorCode,
    FileChange,
    Observation,
    ParsedAction,
    RunState,
    ToolError,
)
from .security import (
    SecurityError,
    authorize_command,
    redact_text,
    resolve_path,
    sanitize_environment,
    validate_command_paths,
)
from .workspace import (
    action_id,
    append_jsonl,
    detect_symlink_escapes,
    diff_snapshots,
    git_status,
    non_git_diff_from_changes,
    scan_workspace,
    snapshot_file,
    truncate_text,
    utc_now,
)
from .workspace import (
    git_diff as workspace_git_diff,
)


class ActionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "read_file",
        "read_many_files",
        "list_files",
        "search",
        "repo_map",
        "run_command",
        "run_tests",
        "apply_patch",
        "write_file",
        "git_diff",
        "request_main_tool",
        "use_skill_context",
        "finish",
    ]
    args: dict[str, Any]


class ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    start_line: int = 1
    max_lines: int = 400


class FileReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    start_line: int = 1
    max_lines: int = 400

    @field_validator("max_lines")
    @classmethod
    def max_lines_limit(cls, value: int) -> int:
        if value < 1 or value > 1000:
            raise ValueError("max_lines must be between 1 and 1000")
        return value


class ReadManyFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    files: list[FileReadRequest]
    total_max_chars: int = 30000

    @field_validator("files")
    @classmethod
    def files_limit(cls, value: list[FileReadRequest]) -> list[FileReadRequest]:
        if not value:
            raise ValueError("files is required")
        if len(value) > 20:
            raise ValueError("read_many_files supports at most 20 files")
        return value

    @field_validator("total_max_chars")
    @classmethod
    def total_chars_limit(cls, value: int) -> int:
        if value < 100 or value > 80000:
            raise ValueError("total_max_chars must be between 100 and 80000")
        return value


class ListFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = "."
    max_entries: int = 500


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    path: str = "."
    glob: str | None = None
    max_results: int = 100


class RepoMapArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    path: str = "."
    max_depth: int = 3
    max_entries: int = 300
    include_hidden: bool = False

    @field_validator("max_depth")
    @classmethod
    def max_depth_limit(cls, value: int) -> int:
        if value < 0 or value > 6:
            raise ValueError("max_depth must be between 0 and 6")
        return value

    @field_validator("max_entries")
    @classmethod
    def max_entries_limit(cls, value: int) -> int:
        if value < 1 or value > 2000:
            raise ValueError("max_entries must be between 1 and 2000")
        return value


class RunCommandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    cmd: str
    timeout_seconds: int = 120
    read_only: bool = False


class RunTestsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    cmd: str
    test_type: Literal["pytest", "npm", "pnpm", "yarn", "cargo", "go", "custom"] = "custom"
    timeout_seconds: int = 120


class ApplyPatchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    patch: str


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    path: str
    content: str
    mode: Literal["create", "overwrite"] = "create"


class GitDiffArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(default_factory=list)


class RequestMainToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    tool: Literal[
        "web_search",
        "fetch_url",
        "browser_snapshot",
        "node_eval",
        "image_generation",
        "skill_context",
        "other",
    ]
    input: dict[str, Any] = Field(default_factory=dict)
    expected_output: str
    sensitivity: Literal["public", "workspace", "private"] = "workspace"


class UseSkillContextArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    item_id: str | None = None
    name: str | None = None
    max_chars: int = 12000

    @field_validator("max_chars")
    @classmethod
    def max_chars_limit(cls, value: int) -> int:
        if value < 100 or value > 50000:
            raise ValueError("max_chars must be between 100 and 50000")
        return value

    @model_validator(mode="after")
    def validate_lookup(self) -> "UseSkillContextArgs":
        if bool(self.item_id) == bool(self.name):
            raise ValueError("provide exactly one of item_id or name")
        return self


class FinishArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["completed", "failed", "waiting_input"]
    summary: str
    self_check_commands: list[str]
    tests: list[str]
    risk_notes: list[str]
    open_issues: list[str]

    @model_validator(mode="after")
    def validate_finish(self) -> "FinishArgs":
        if not self.summary.strip():
            raise ValueError("finish.summary is required")
        if self.status == "completed" and self.open_issues:
            raise ValueError("completed finish cannot include open_issues")
        if self.status == "completed" and not (self.self_check_commands or self.tests or self.risk_notes):
            raise ValueError("completed finish must include verification or risk notes")
        return self


ACTION_ARG_MODELS = {
    "read_file": ReadFileArgs,
    "read_many_files": ReadManyFilesArgs,
    "list_files": ListFilesArgs,
    "search": SearchArgs,
    "repo_map": RepoMapArgs,
    "run_command": RunCommandArgs,
    "run_tests": RunTestsArgs,
    "apply_patch": ApplyPatchArgs,
    "write_file": WriteFileArgs,
    "git_diff": GitDiffArgs,
    "request_main_tool": RequestMainToolArgs,
    "use_skill_context": UseSkillContextArgs,
    "finish": FinishArgs,
}


ACTION_TOOL_GUIDANCE = {
    "read_file": {
        "when_to_use": "Read a small, specific file range after narrowing the path.",
        "when_not_to_use": "Do not repeatedly read many files one by one; use read_many_files.",
        "after_call_behavior": "Use the observation as evidence before editing or finishing.",
    },
    "read_many_files": {
        "when_to_use": "Read several known files in one step after repo_map/search identifies them.",
        "when_not_to_use": "Do not use for sensitive paths, external roots that were not allowed, or huge files.",
        "after_call_behavior": "Summarize evidence or continue with the next minimal action.",
    },
    "list_files": {
        "when_to_use": "Inspect a single directory when repo shape is unknown.",
        "when_not_to_use": "Do not crawl broad trees with repeated list_files; use repo_map.",
        "after_call_behavior": "Pick a narrower path or finish if enough evidence exists.",
    },
    "search": {
        "when_to_use": "Find exact symbols, strings, filenames, or config keys.",
        "when_not_to_use": "Do not search from repository root without a narrowed path/glob when avoidable.",
        "after_call_behavior": "Read matching files before drawing conclusions.",
    },
    "repo_map": {
        "when_to_use": "Build a quick project map before exploratory reading or broad refactors.",
        "when_not_to_use": "Do not use to expose sensitive paths or bypass allowed_external_roots.",
        "after_call_behavior": "Use read_many_files/search on the key files it identifies.",
    },
    "run_command": {
        "when_to_use": "Use only when structured actions cannot answer the question or perform the task.",
        "when_not_to_use": "Do not start with broad find/grep over caches, .conda, .venv, node_modules, or large data.",
        "after_call_behavior": "Inspect stdout/stderr paths and command_effects before continuing.",
    },
    "run_tests": {
        "when_to_use": "Run a focused verification command after making or evaluating code changes.",
        "when_not_to_use": "Do not use as a broad shell escape; keep the command focused and explain reason first.",
        "after_call_behavior": "Use exit code, logs, and command effects as verification evidence.",
    },
    "apply_patch": {
        "when_to_use": "Apply scoped textual edits after reading relevant files.",
        "when_not_to_use": "Do not patch before reading context or for generated binary/large artifacts.",
        "after_call_behavior": "Check changed_files and run focused verification if needed.",
    },
    "write_file": {
        "when_to_use": "Create or overwrite a targeted text file when patch is not appropriate.",
        "when_not_to_use": "Do not overwrite existing files without clear reason and prior context.",
        "after_call_behavior": "Verify the written path and include it in final summary.",
    },
    "git_diff": {
        "when_to_use": "Inspect current workspace changes before finalizing worker results.",
        "when_not_to_use": "Do not use as a substitute for reading files when no edits were made.",
        "after_call_behavior": "Use the diff as final audit evidence.",
    },
    "request_main_tool": {
        "when_to_use": "Ask the MCP host or orchestrator to call web, browser, node, image, skill, or other tools you do not own.",
        "when_not_to_use": "Do not fabricate external tool results or request tools for ordinary file reads.",
        "after_call_behavior": "Stop. The run enters waiting_input until Codex sends the tool result back.",
    },
    "use_skill_context": {
        "when_to_use": "Read a host-injected skill/context item before acting on specialized instructions.",
        "when_not_to_use": "Do not scan skill directories yourself or guess skill rules from memory.",
        "after_call_behavior": "Follow the retrieved context or request clarification if insufficient.",
    },
    "finish": {
        "when_to_use": "End the run with status, summary, verification, risk notes, and open issues.",
        "when_not_to_use": "Do not finish with empty evidence, hidden failures, or unresolved required context.",
        "after_call_behavior": "No further action.",
    },
}


REPO_MAP_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".runs",
    ".venv",
    "venv",
    ".conda",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "node_modules",
    "dist",
    "build",
    "target",
    "coverage",
}

KEY_FILE_NAMES = {
    "README.md",
    "AGENTS.md",
    "pyproject.toml",
    "package.json",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "Cargo.toml",
    "go.mod",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    "Makefile",
    "tsconfig.json",
    "vite.config.ts",
}


class ActionParseError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def parse_action(raw_output: str) -> ParsedAction:
    payload, _repaired = _load_action_payload(raw_output)
    try:
        envelope = ActionEnvelope.model_validate(payload)
        arg_model = ACTION_ARG_MODELS[envelope.action]
        args = arg_model.model_validate(envelope.args)
    except ValidationError as exc:
        raise ActionParseError(
            ErrorCode.action_schema_error,
            "action schema validation failed",
            {"errors": exc.errors(include_context=False)},
        ) from exc
    return ParsedAction(action_id=action_id(), action=envelope.action, args=args.model_dump(mode="json"))


def _load_action_payload(raw_output: str) -> tuple[dict[str, Any], bool]:
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        first_error = exc
    else:
        if isinstance(payload, dict):
            return payload, False
        raise ActionParseError(
            ErrorCode.model_output_parse_error,
            "model output JSON must be an object",
            {"type": type(payload).__name__},
        )

    for candidate in _json_repair_candidates(raw_output):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload, True
    raise ActionParseError(ErrorCode.model_output_parse_error, str(first_error)) from first_error


def _json_repair_candidates(raw_output: str) -> list[str]:
    stripped = raw_output.strip()
    candidates: list[str] = []
    fenced = _strip_markdown_fence(stripped)
    if fenced != stripped:
        candidates.append(fenced)
    extracted = _extract_first_json_object(stripped)
    if extracted and extracted not in candidates:
        candidates.append(extracted)
    if fenced != stripped:
        fenced_extracted = _extract_first_json_object(fenced)
        if fenced_extracted and fenced_extracted not in candidates:
            candidates.append(fenced_extracted)

    repaired: list[str] = []
    for candidate in candidates:
        repaired.append(candidate)
        without_trailing_commas = re.sub(r",(\s*[}\]])", r"\1", candidate)
        if without_trailing_commas != candidate:
            repaired.append(without_trailing_commas)
    return repaired


def _strip_markdown_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```"):
        if lines[-1].strip().startswith("```"):
            return "\n".join(lines[1:-1]).strip()
        return "\n".join(lines[1:]).strip()
    return text


def _extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1].strip()
    return None


def observation_ok(action_id_value: str, data: dict[str, Any], *, truncated: bool = False, artifacts: list[dict[str, Any]] | None = None) -> Observation:
    clean = redact_value(data)
    return Observation(action_id=action_id_value, ok=True, data=clean, truncated=truncated, artifacts=artifacts or [])


def observation_error(action_id_value: str, code: ErrorCode, message: str, details: dict[str, Any] | None = None) -> Observation:
    clean_details = redact_value(details or {})
    return Observation(
        action_id=action_id_value,
        ok=False,
        error=ToolError(code=code, message=message, details=clean_details),
    )


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value


class ActionExecutor:
    def __init__(
        self,
        *,
        settings: Settings,
        lock_manager: RepositoryLockManager,
        active_processes: dict[str, asyncio.subprocess.Process],
    ) -> None:
        self.settings = settings
        self.lock_manager = lock_manager
        self.active_processes = active_processes

    async def execute(self, state: RunState, parsed: ParsedAction) -> Observation:
        try:
            if parsed.action == "read_file":
                return await self._read_file(state, parsed)
            if parsed.action == "read_many_files":
                return await self._read_many_files(state, parsed)
            if parsed.action == "list_files":
                return await self._list_files(state, parsed)
            if parsed.action == "search":
                return await self._search(state, parsed)
            if parsed.action == "repo_map":
                return await self._repo_map(state, parsed)
            if parsed.action == "run_command":
                return await self._run_command(state, parsed)
            if parsed.action == "run_tests":
                return await self._run_tests(state, parsed)
            if parsed.action == "apply_patch":
                return await self._apply_patch(state, parsed)
            if parsed.action == "write_file":
                return await self._write_file(state, parsed)
            if parsed.action == "git_diff":
                return await self._git_diff(state, parsed)
            if parsed.action == "request_main_tool":
                return await self._request_main_tool(state, parsed)
            if parsed.action == "use_skill_context":
                return await self._use_skill_context(state, parsed)
            if parsed.action == "finish":
                return observation_ok(parsed.action_id, parsed.args)
        except SecurityError as exc:
            return Observation(action_id=parsed.action_id, ok=False, error=exc.error)
        except Exception as exc:
            return observation_error(parsed.action_id, ErrorCode.internal_error, str(exc))
        return observation_error(parsed.action_id, ErrorCode.action_not_allowed, f"unknown action {parsed.action}")

    def _roots(self, state: RunState) -> tuple[Path, list[Path]]:
        cwd = Path(state.cwd).resolve()
        external = [Path(p).resolve() for p in state.allowed_external_roots]
        return cwd, external

    def _resolve(self, state: RunState, path: str, *, operation: str) -> Path:
        cwd, external = self._roots(state)
        return resolve_path(
            path,
            cwd,
            external,
            operation=operation,
            explicit_authorizations=state.explicit_authorizations,
            sensitive_patterns=self.settings.sensitive_path_patterns,
        )

    async def _read_file(self, state: RunState, parsed: ParsedAction) -> Observation:
        args = ReadFileArgs.model_validate(parsed.args)
        path = self._resolve(state, args.path, operation="read")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(args.start_line, 1) - 1
        selected = lines[start : start + args.max_lines]
        truncated = start + args.max_lines < len(lines)
        return observation_ok(
            parsed.action_id,
            {
                "path": str(path),
                "start_line": start + 1,
                "end_line": start + len(selected),
                "content": "\n".join(selected),
            },
            truncated=truncated,
        )

    async def _read_many_files(self, state: RunState, parsed: ParsedAction) -> Observation:
        args = ReadManyFilesArgs.model_validate(parsed.args)
        remaining = args.total_max_chars
        results: list[dict[str, Any]] = []
        truncated = False
        for request in args.files:
            try:
                path = self._resolve(state, request.path, operation="read")
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                start = max(request.start_line, 1) - 1
                selected = lines[start : start + request.max_lines]
                content = "\n".join(selected)
                file_truncated = start + request.max_lines < len(lines)
                if remaining <= 0:
                    content = ""
                    file_truncated = True
                    truncated = True
                elif len(content) > remaining:
                    content = content[:remaining] + "\n[truncated]"
                    file_truncated = True
                    truncated = True
                    remaining = 0
                else:
                    remaining -= len(content)
                results.append(
                    {
                        "ok": True,
                        "path": str(path),
                        "start_line": start + 1,
                        "end_line": start + len(selected),
                        "content": content,
                        "truncated": file_truncated,
                    }
                )
            except SecurityError as exc:
                results.append({"ok": False, "path": request.path, "error": exc.error.model_dump(mode="json")})
            except Exception as exc:
                results.append(
                    {
                        "ok": False,
                        "path": request.path,
                        "error": {"code": ErrorCode.io_error.value, "message": str(exc), "details": {}},
                    }
                )
        return observation_ok(parsed.action_id, {"reason": args.reason, "files": results}, truncated=truncated)

    async def _list_files(self, state: RunState, parsed: ParsedAction) -> Observation:
        args = ListFilesArgs.model_validate(parsed.args)
        path = self._resolve(state, args.path, operation="read")
        entries = []
        for child in sorted(path.iterdir(), key=lambda p: p.name)[: args.max_entries]:
            entries.append({"name": child.name, "type": "dir" if child.is_dir() else "file"})
        truncated = len(list(path.iterdir())) > args.max_entries
        return observation_ok(parsed.action_id, {"path": str(path), "entries": entries}, truncated=truncated)

    async def _search(self, state: RunState, parsed: ParsedAction) -> Observation:
        args = SearchArgs.model_validate(parsed.args)
        path = self._resolve(state, args.path, operation="read")
        cmd = ["rg", "--line-number", "--no-heading", args.query, str(path)]
        if args.glob:
            cmd[1:1] = ["--glob", args.glob]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            raw_lines = stdout.decode("utf-8", errors="replace").splitlines()
        except Exception:
            raw_lines = []
            for file in path.rglob("*"):
                if not file.is_file():
                    continue
                if args.glob and not file.match(args.glob):
                    continue
                try:
                    self._resolve(state, str(file), operation="read")
                    for index, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                        if args.query in line:
                            raw_lines.append(f"{file}:{index}:{line}")
                except (OSError, SecurityError):
                    continue
        matches = raw_lines[: args.max_results]
        return observation_ok(parsed.action_id, {"matches": matches}, truncated=len(raw_lines) > len(matches))

    async def _repo_map(self, state: RunState, parsed: ParsedAction) -> Observation:
        args = RepoMapArgs.model_validate(parsed.args)
        root = self._resolve(state, args.path, operation="read")
        ignore_dirs = set(REPO_MAP_IGNORE_DIRS) | set(self.settings.snapshot_ignore_dirs)
        tree: list[dict[str, Any]] = []
        key_files: list[dict[str, str]] = []
        language_stats: dict[str, int] = {}
        entry_candidates: list[str] = []
        symlink_escapes: list[str] = []
        visited = 0
        truncated = False

        def should_skip(path: Path) -> bool:
            if path.name in ignore_dirs:
                return True
            return not args.include_hidden and path.name.startswith(".")

        for current, dirnames, filenames in os.walk(root):
            current_path = Path(current)
            try:
                rel_dir = "." if current_path == root else current_path.relative_to(root).as_posix()
            except ValueError:
                continue
            depth = 0 if rel_dir == "." else len(Path(rel_dir).parts)
            if depth > args.max_depth:
                dirnames[:] = []
                continue
            kept_dirs = []
            for dirname in sorted(dirnames):
                child = current_path / dirname
                if should_skip(child):
                    continue
                if child.is_symlink():
                    target = child.resolve(strict=False)
                    try:
                        target.relative_to(root)
                    except ValueError:
                        symlink_escapes.append(child.relative_to(root).as_posix())
                        continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
            for child in [current_path / d for d in kept_dirs] + [current_path / f for f in sorted(filenames)]:
                if visited >= args.max_entries:
                    truncated = True
                    break
                if should_skip(child):
                    continue
                if child.is_symlink():
                    target = child.resolve(strict=False)
                    try:
                        target.relative_to(root)
                    except ValueError:
                        symlink_escapes.append(child.relative_to(root).as_posix())
                        continue
                try:
                    rel = child.relative_to(root).as_posix()
                except ValueError:
                    continue
                kind = "dir" if child.is_dir() else "file"
                tree.append({"path": rel, "type": kind})
                visited += 1
                if child.is_file():
                    suffix = child.suffix.lower() or "[no extension]"
                    language_stats[suffix] = language_stats.get(suffix, 0) + 1
                    if child.name in KEY_FILE_NAMES:
                        key_files.append({"path": rel, "reason": "known project metadata"})
                    if child.name in {"main.py", "app.py", "index.ts", "index.js", "server.ts", "server.js"}:
                        entry_candidates.append(rel)
            if truncated:
                break
        return observation_ok(
            parsed.action_id,
            {
                "reason": args.reason,
                "root": str(root),
                "tree": tree,
                "key_files": key_files[:50],
                "entry_candidates": entry_candidates[:50],
                "language_stats": dict(sorted(language_stats.items())),
                "symlink_escapes_skipped": sorted(set(symlink_escapes)),
            },
            truncated=truncated,
        )

    async def _run_command(self, state: RunState, parsed: ParsedAction) -> Observation:
        args = RunCommandArgs.model_validate(parsed.args)
        if state.agent_type == AgentType.explorer and not args.read_only:
            return observation_error(parsed.action_id, ErrorCode.action_not_allowed, "explorer cannot run write commands")
        effective_read_only, risks = authorize_command(
            args.cmd,
            read_only=args.read_only,
            timeout_seconds=args.timeout_seconds,
            default_timeout_seconds=self.settings.default_command_timeout_seconds,
            explicit_authorizations=state.explicit_authorizations,
        )
        cwd, external = self._roots(state)
        validate_command_paths(
            args.cmd,
            cwd=cwd,
            allowed_external_roots=external,
            operation="read" if effective_read_only else "write",
            explicit_authorizations=state.explicit_authorizations,
            sensitive_patterns=self.settings.sensitive_path_patterns,
        )

        async def run() -> Observation:
            return await self._run_command_locked(state, parsed, args, risks, effective_read_only)

        if effective_read_only:
            return await run()
        return await self.lock_manager.run_with_write_lock(Path(state.cwd), run)

    async def _run_command_locked(
        self,
        state: RunState,
        parsed: ParsedAction,
        args: RunCommandArgs,
        risks: set[str],
        effective_read_only: bool,
    ) -> Observation:
        cwd = Path(state.cwd).resolve()
        run_dir = Path(state.run_dir)
        before = scan_workspace(
            cwd,
            run_dir=run_dir,
            snapshot_name=f"{parsed.action_id}_before",
            ignore_dirs=self.settings.snapshot_ignore_dirs,
        )
        before_symlink_escapes = detect_symlink_escapes(
            cwd,
            ignore_dirs=self.settings.snapshot_ignore_dirs,
        )
        if before_symlink_escapes and not effective_read_only:
            return observation_error(
                parsed.action_id,
                ErrorCode.path_escape_blocked,
                "write command blocked because workspace contains symlink escapes",
                {"symlinks": before_symlink_escapes},
            )
        before_git = git_status(cwd)
        logs_dir = run_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = logs_dir / f"{parsed.action_id}.stdout"
        stderr_path = logs_dir / f"{parsed.action_id}.stderr"
        timed_out = False
        process = await asyncio.create_subprocess_shell(
            args.cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=sanitize_environment(),
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        self.active_processes[state.agent_id] = process
        try:
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=args.timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                await terminate_process_group(process)
                stdout, stderr = await process.communicate()
        finally:
            self.active_processes.pop(state.agent_id, None)
        stdout_path.write_bytes(stdout or b"")
        stderr_path.write_bytes(stderr or b"")
        after = scan_workspace(
            cwd,
            run_dir=run_dir,
            snapshot_name=f"{parsed.action_id}_after",
            ignore_dirs=self.settings.snapshot_ignore_dirs,
        )
        after_symlink_escapes = detect_symlink_escapes(
            cwd,
            ignore_dirs=self.settings.snapshot_ignore_dirs,
        )
        after_git = git_status(cwd)
        changes = diff_snapshots(
            before,
            after,
            agent_id=state.agent_id,
            segment_id=state.current_segment_id,
            action_id=parsed.action_id,
            source="command",
        )
        for change in changes:
            append_jsonl(run_dir / "command_effects.jsonl", change.model_dump(mode="json"))
        stdout_text, stdout_truncated = truncate_text(stdout.decode("utf-8", errors="replace"), self.settings.log_truncate_chars)
        stderr_text, stderr_truncated = truncate_text(stderr.decode("utf-8", errors="replace"), self.settings.log_truncate_chars)
        record = {
            "agent_id": state.agent_id,
            "segment_id": state.current_segment_id,
            "action_id": parsed.action_id,
            "cmd": args.cmd,
            "returncode": process.returncode,
            "timed_out": timed_out,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "risks": sorted(risks),
            "process_group_id": process.pid,
            "before_git_status": before_git,
            "after_git_status": after_git,
        }
        append_jsonl(run_dir / "commands.jsonl", record)
        if after_symlink_escapes and not before_symlink_escapes:
            return observation_error(
                parsed.action_id,
                ErrorCode.path_escape_blocked,
                "command created symlink escape",
                {**record, "symlinks": after_symlink_escapes},
            )
        if timed_out:
            return observation_error(parsed.action_id, ErrorCode.timeout_exceeded, "command timed out", record)
        return observation_ok(
            parsed.action_id,
            {
                **record,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "command_effects": [c.model_dump(mode="json") for c in changes],
            },
            truncated=stdout_truncated or stderr_truncated,
        )

    async def _run_tests(self, state: RunState, parsed: ParsedAction) -> Observation:
        if state.agent_type != AgentType.worker:
            return observation_error(parsed.action_id, ErrorCode.action_not_allowed, "run_tests requires worker")
        args = RunTestsArgs.model_validate(parsed.args)
        command_args = RunCommandArgs(
            reason=args.reason,
            cmd=args.cmd,
            timeout_seconds=args.timeout_seconds,
            read_only=False,
        )
        command_observation = await self._run_command(
            state,
            ParsedAction(action_id=parsed.action_id, action="run_command", args=command_args.model_dump(mode="json")),
        )
        if not command_observation.ok:
            return command_observation
        return observation_ok(
            parsed.action_id,
            {
                "reason": args.reason,
                "test_type": args.test_type,
                "cmd": args.cmd,
                **(command_observation.data or {}),
            },
            truncated=command_observation.truncated,
            artifacts=command_observation.artifacts,
        )

    async def _apply_patch(self, state: RunState, parsed: ParsedAction) -> Observation:
        if state.agent_type != AgentType.worker:
            return observation_error(parsed.action_id, ErrorCode.action_not_allowed, "only worker can apply patches")
        args = ApplyPatchArgs.model_validate(parsed.args)
        if state.dry_run:
            return observation_ok(parsed.action_id, {"dry_run": True, "patch": args.patch})
        for patch_path in patch_target_paths(args.patch):
            self._resolve(state, patch_path, operation="write")

        async def run() -> Observation:
            cwd = Path(state.cwd).resolve()
            run_dir = Path(state.run_dir)
            before = scan_workspace(
                cwd,
                run_dir=run_dir,
                snapshot_name=f"{parsed.action_id}_before",
                ignore_dirs=self.settings.snapshot_ignore_dirs,
            )
            proc = await asyncio.create_subprocess_exec(
                "git",
                "apply",
                "--whitespace=nowarn",
                "-",
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(args.patch.encode("utf-8"))
            if proc.returncode != 0:
                return observation_error(
                    parsed.action_id,
                    ErrorCode.io_error,
                    "git apply failed",
                    {"stderr": stderr.decode("utf-8", errors="replace")},
                )
            after = scan_workspace(
                cwd,
                run_dir=run_dir,
                snapshot_name=f"{parsed.action_id}_after",
                ignore_dirs=self.settings.snapshot_ignore_dirs,
            )
            changes = diff_snapshots(
                before,
                after,
                agent_id=state.agent_id,
                segment_id=state.current_segment_id,
                action_id=parsed.action_id,
                source="write",
            )
            for change in changes:
                append_jsonl(run_dir / "writes.jsonl", change.model_dump(mode="json"))
            return observation_ok(parsed.action_id, {"changed_files": [c.path for c in changes], "stdout": stdout.decode("utf-8", errors="replace")})

        return await self.lock_manager.run_with_write_lock(Path(state.cwd), run)

    async def _write_file(self, state: RunState, parsed: ParsedAction) -> Observation:
        if state.agent_type != AgentType.worker:
            return observation_error(parsed.action_id, ErrorCode.action_not_allowed, "only worker can write files")
        args = WriteFileArgs.model_validate(parsed.args)
        target = self._resolve(state, args.path, operation="write")
        if args.mode == "create" and target.exists():
            return observation_error(parsed.action_id, ErrorCode.io_error, "target exists")
        if state.dry_run:
            return observation_ok(parsed.action_id, {"dry_run": True, "path": str(target)})

        async def run() -> Observation:
            cwd = Path(state.cwd).resolve()
            run_dir = Path(state.run_dir)
            rel = target.relative_to(cwd).as_posix()
            before_entry = snapshot_file(target, rel_path=rel, content_dir=run_dir / "snapshots" / f"{parsed.action_id}_before")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args.content, encoding="utf-8")
            after_entry = snapshot_file(target, rel_path=rel, content_dir=run_dir / "snapshots" / f"{parsed.action_id}_after")
            effect = "created" if not before_entry.exists else "modified"
            change = FileChange(
                agent_id=state.agent_id,
                segment_id=state.current_segment_id,
                action_id=parsed.action_id,
                path=rel,
                effect=effect,  # type: ignore[arg-type]
                before=before_entry,
                after=after_entry,
                timestamp=utc_now(),
                source="write",
            )
            append_jsonl(run_dir / "writes.jsonl", change.model_dump(mode="json"))
            return observation_ok(parsed.action_id, {"path": str(target), "effect": effect})

        return await self.lock_manager.run_with_write_lock(Path(state.cwd), run)

    async def _git_diff(self, state: RunState, parsed: ParsedAction) -> Observation:
        args = GitDiffArgs.model_validate(parsed.args)
        cwd = Path(state.cwd).resolve()
        diff = workspace_git_diff(cwd, args.paths)
        if not diff:
            changes = [
                FileChange.model_validate(row)
                for row in read_change_rows(Path(state.run_dir))
            ]
            diff = non_git_diff_from_changes(changes)
        diff_path = Path(state.run_dir) / "changes.diff"
        diff_path.write_text(diff, encoding="utf-8")
        return observation_ok(parsed.action_id, {"diff": diff, "diff_path": str(diff_path)})

    async def _request_main_tool(self, state: RunState, parsed: ParsedAction) -> Observation:
        args = RequestMainToolArgs.model_validate(parsed.args)
        request = {
            "reason": args.reason,
            "tool": args.tool,
            "input": args.input,
            "expected_output": args.expected_output,
            "sensitivity": args.sensitivity,
        }
        return observation_ok(parsed.action_id, {"requested_main_tool": request})

    async def _use_skill_context(self, state: RunState, parsed: ParsedAction) -> Observation:
        args = UseSkillContextArgs.model_validate(parsed.args)
        items_path = Path(state.run_dir) / "items.jsonl"
        rows = []
        if items_path.exists():
            for line in items_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        match = None
        for row in rows:
            if args.item_id and row.get("item_id") == args.item_id:
                match = row
                break
            if args.name and row.get("name") == args.name:
                match = row
                break
        if match is None:
            return observation_error(
                parsed.action_id,
                ErrorCode.invalid_request,
                "context item not found",
                {"item_id": args.item_id, "name": args.name, "items_path": str(items_path)},
            )
        if match.get("type") != "text":
            return observation_error(
                parsed.action_id,
                ErrorCode.invalid_request,
                "use_skill_context only supports text items; use read_file for local_path items",
                {"item_id": match.get("item_id"), "type": match.get("type")},
            )
        content, truncated = truncate_text(str(match.get("text", "")), args.max_chars)
        return observation_ok(
            parsed.action_id,
            {
                "reason": args.reason,
                "item_id": match.get("item_id"),
                "name": match.get("name"),
                "content": content,
                "items_path": str(items_path),
            },
            truncated=truncated,
        )


async def terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if hasattr(os, "killpg") and process.pid:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            if hasattr(os, "killpg") and process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            await process.wait()
    except ProcessLookupError:
        return


def patch_target_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            raw = line[4:].strip()
            raw = raw.split("\t", 1)[0].strip()
            if raw == "/dev/null":
                continue
            if raw.startswith("a/") or raw.startswith("b/"):
                raw = raw[2:]
            paths.append(raw)
    return paths


def read_change_rows(run_dir: Path) -> list[dict[str, Any]]:
    from .workspace import read_jsonl

    return read_jsonl(run_dir / "writes.jsonl") + read_jsonl(run_dir / "command_effects.jsonl")
