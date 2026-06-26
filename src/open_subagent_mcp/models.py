from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ErrorCode(str, Enum):
    invalid_request = "invalid_request"
    unknown_agent = "unknown_agent"
    invalid_state = "invalid_state"
    unsupported_item_type = "unsupported_item_type"
    path_escape_blocked = "path_escape_blocked"
    sensitive_path_blocked = "sensitive_path_blocked"
    authorization_required = "authorization_required"
    dangerous_command_blocked = "dangerous_command_blocked"
    timeout_exceeded = "timeout_exceeded"
    llm_http_error = "llm_http_error"
    llm_truncated = "llm_truncated"
    model_output_parse_error = "model_output_parse_error"
    action_schema_error = "action_schema_error"
    action_not_allowed = "action_not_allowed"
    rollback_conflict = "rollback_conflict"
    io_error = "io_error"
    internal_error = "internal_error"


class AgentType(str, Enum):
    explorer = "explorer"
    worker = "worker"


class AgentStatus(str, Enum):
    created = "created"
    queued = "queued"
    running = "running"
    waiting_input = "waiting_input"
    completed = "completed"
    failed = "failed"
    crashed = "crashed"
    interrupted = "interrupted"
    closing = "closing"
    closed = "closed"
    rolled_back = "rolled_back"
    partially_rolled_back = "partially_rolled_back"


class Authorization(str, Enum):
    install_dependencies = "install_dependencies"
    read_external_roots = "read_external_roots"
    long_running_commands = "long_running_commands"
    destructive_actions = "destructive_actions"
    git_commit = "git_commit"
    git_push = "git_push"
    system_config = "system_config"
    production_operations = "production_operations"


TERMINAL_STATUSES = {
    AgentStatus.completed,
    AgentStatus.failed,
    AgentStatus.crashed,
    AgentStatus.interrupted,
    AgentStatus.closed,
    AgentStatus.rolled_back,
    AgentStatus.partially_rolled_back,
}


class ToolError(BaseModel):
    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResponse(BaseModel):
    ok: bool
    data: dict[str, Any] | None = None
    error: ToolError | None = None


def ok(data: dict[str, Any]) -> dict[str, Any]:
    return ToolResponse(ok=True, data=data).model_dump(mode="json", exclude_none=True)


def err(code: ErrorCode, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return ToolResponse(
        ok=False, error=ToolError(code=code, message=message, details=details or {})
    ).model_dump(mode="json", exclude_none=True)


class TextItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"]
    name: str | None = None
    text: str


class LocalPathItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["local_path"]
    name: str | None = None
    path: str


InputItem = TextItem | LocalPathItem


class SpawnAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_type: AgentType
    message: str
    cwd: str
    items: list[dict[str, Any]] = Field(default_factory=list)
    fork_context: bool = False
    model: str | None = None
    dry_run: bool = False
    max_steps: int = 160
    timeout_seconds: int = 120
    allowed_external_roots: list[str] = Field(default_factory=list)
    explicit_authorizations: list[Authorization] = Field(default_factory=list)

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message is required")
        return value

    @field_validator("max_steps")
    @classmethod
    def max_steps_limit(cls, value: int) -> int:
        if value < 1 or value > 200:
            raise ValueError("max_steps must be between 1 and 200")
        return value

    @model_validator(mode="after")
    def long_timeout_requires_auth(self) -> "SpawnAgentRequest":
        if (
            self.timeout_seconds > 120
            and Authorization.long_running_commands not in self.explicit_authorizations
        ):
            raise ValueError("timeout_seconds > 120 requires long_running_commands")
        return self


class WaitAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targets: list[str]
    timeout_ms: int = 30000


class SendInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str
    message: str
    items: list[dict[str, Any]] = Field(default_factory=list)
    interrupt: bool = False


class CloseAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str


class RollbackAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str
    segment_id: str | None = None
    paths: list[str] = Field(default_factory=list)
    include_command_effects: bool = True
    force: bool = False


class SegmentRecord(BaseModel):
    segment_id: str
    reason: str
    started_at: str
    ended_at: str | None = None


class RunState(BaseModel):
    agent_id: str
    status: AgentStatus
    agent_type: AgentType
    cwd: str
    run_dir: str
    current_segment_id: str
    model: str
    dry_run: bool = False
    max_steps: int = 160
    timeout_seconds: int = 120
    allowed_external_roots: list[str] = Field(default_factory=list)
    explicit_authorizations: list[Authorization] = Field(default_factory=list)
    step_count: int = 0
    format_error_count: int = 0
    final_message: str = ""
    failure_reason: str | None = None
    parse_warning: str | None = None
    raw_output_path: str | None = None
    parse_error_path: str | None = None
    error_path: str | None = None
    last_error: dict[str, Any] | None = None
    last_model_event: dict[str, Any] | None = None
    created_at: str
    updated_at: str
    ended_at: str | None = None
    queued_inputs: list[dict[str, Any]] = Field(default_factory=list)


class FileSnapshot(BaseModel):
    path: str
    type: Literal["file", "dir", "symlink", "missing"]
    exists: bool
    size: int | None = None
    mode: int | None = None
    sha256: str | None = None
    mtime: float | None = None
    content_path: str | None = None
    link_target: str | None = None


class FileChange(BaseModel):
    agent_id: str
    segment_id: str
    action_id: str
    path: str
    effect: Literal["created", "modified", "deleted", "mode_changed", "type_changed"]
    before: FileSnapshot
    after: FileSnapshot
    timestamp: str
    source: Literal["write", "command"]


class Observation(BaseModel):
    type: Literal["observation"] = "observation"
    action_id: str
    ok: bool
    data: dict[str, Any] | None = None
    error: ToolError | None = None
    truncated: bool = False
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class ParsedAction(BaseModel):
    action_id: str
    action: str
    args: dict[str, Any]


def path_json(path: Path | str | None) -> str | None:
    if path is None:
        return None
    return str(path)
