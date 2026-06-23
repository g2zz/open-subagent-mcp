from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .actions import ActionExecutor, ActionParseError, parse_action
from .config import Settings, load_settings
from .llm_client import ChatClient, LLMError, OpenAICompatibleClient
from .locks import RepositoryLockManager
from .models import (
    TERMINAL_STATUSES,
    AgentStatus,
    Authorization,
    ErrorCode,
    LocalPathItem,
    RunState,
    SegmentRecord,
    SpawnAgentRequest,
    TextItem,
    err,
    ok,
)
from .prompts import SYSTEM_PROMPT, build_repair_prompt, build_task_package
from .rollback import rollback_run
from .security import SecurityError, normalize_roots, redact_text, resolve_path
from .workspace import append_jsonl, git_diff, read_json, read_jsonl, truncate_text, utc_now, write_json

TRANSIENT_STATUSES = {
    AgentStatus.created,
    AgentStatus.queued,
    AgentStatus.running,
    AgentStatus.waiting_input,
    AgentStatus.closing,
}

FINAL_MESSAGE_LIMIT = 16000
MODEL_OUTPUT_PREVIEW_LIMIT = 2000


def _status_payload(state: RunState) -> dict[str, Any]:
    return {
        "run_id": state.agent_id,
        "status": state.status.value,
        "started_at": state.created_at,
        "updated_at": state.updated_at,
        "current_step": state.step_count,
        "failure_reason": state.failure_reason,
        "last_error": state.last_error,
        "last_model_event": state.last_model_event,
        "raw_output_path": state.raw_output_path,
        "parse_error_path": state.parse_error_path,
        "error_path": state.error_path,
    }


def _validation_error_details(exc: ValidationError) -> dict[str, Any]:
    errors = exc.errors(include_context=False)
    details: dict[str, Any] = {"errors": errors}
    for item in errors:
        loc = tuple(item.get("loc", ()))
        message = str(item.get("msg", ""))
        if loc == ("agent_type",):
            details["hint"] = 'agent_type must be "explorer" or "worker"'
        elif "timeout_seconds > 120 requires long_running_commands" in message:
            details["hint"] = 'timeout_seconds > 120 requires explicit_authorizations=["long_running_commands"]'
            details["authorization_required"] = Authorization.long_running_commands.value
    return details


class RunStore:
    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, agent_id: str) -> Path:
        return self.runs_dir / agent_id

    def exists(self, agent_id: str) -> bool:
        return (self.run_dir(agent_id) / "state.json").exists()

    def load_state(self, agent_id: str) -> RunState:
        path = self.run_dir(agent_id) / "state.json"
        if not path.exists():
            raise KeyError(agent_id)
        return RunState.model_validate(read_json(path))

    def save_state(self, state: RunState) -> None:
        state.updated_at = utc_now()
        run_dir = self.run_dir(state.agent_id)
        write_json(run_dir / "state.json", state.model_dump(mode="json"))
        write_json(run_dir / "status.json", _status_payload(state))

    def append_event(self, agent_id: str, event: str, data: dict[str, Any] | None = None) -> None:
        append_jsonl(
            self.run_dir(agent_id) / "events.jsonl",
            {"timestamp": utc_now(), "event": event, "data": data or {}},
        )

    def write_error(
        self,
        state: RunState,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        run_dir = self.run_dir(state.agent_id)
        payload = {
            "run_id": state.agent_id,
            "status": state.status.value,
            "failure_reason": reason,
            "message": message,
            "details": details or {},
            "last_model_event": state.last_model_event,
            "timestamp": utc_now(),
        }
        write_json(run_dir / "error.json", payload)
        state.error_path = str(run_dir / "error.json")
        state.last_error = payload

    def append_segment(self, state: RunState, segment: SegmentRecord) -> None:
        append_jsonl(self.run_dir(state.agent_id) / "segments.jsonl", segment.model_dump(mode="json"))
        state.current_segment_id = segment.segment_id
        self.save_state(state)

    def list_segments(self, agent_id: str) -> list[dict[str, Any]]:
        return read_jsonl(self.run_dir(agent_id) / "segments.jsonl")

    def mark_orphans_interrupted(self) -> None:
        for child in self.runs_dir.iterdir():
            state_path = child / "state.json"
            if not state_path.exists():
                continue
            state = RunState.model_validate(read_json(state_path))
            if state.status in TRANSIENT_STATUSES:
                state.status = AgentStatus.interrupted
                state.failure_reason = "interrupted_after_restart"
                state.ended_at = utc_now()
                self.write_error(
                    state,
                    reason=state.failure_reason,
                    message="Open Subagent MCP service restarted before this run reached a final result.",
                    details={"previous_status": read_json(state_path).get("status")},
                )
                self.save_state(state)
                self.append_event(
                    state.agent_id,
                    "interrupted_after_restart",
                    {"previous_status": state.last_error.get("details", {}).get("previous_status") if state.last_error else None},
                )


class OpenSubagentRuntime:
    def __init__(self, settings: Settings | None = None, llm_client: ChatClient | None = None) -> None:
        self.settings = settings or load_settings()
        self.store = RunStore(self.settings.runs_dir)
        self.store.mark_orphans_interrupted()
        self.llm_client = llm_client or OpenAICompatibleClient(self.settings)
        self.lock_manager = RepositoryLockManager()
        self.active_processes: dict[str, asyncio.subprocess.Process] = {}
        self.executor = ActionExecutor(
            settings=self.settings,
            lock_manager=self.lock_manager,
            active_processes=self.active_processes,
        )
        self._tasks: dict[str, asyncio.Task] = {}
        self._pending: list[str] = []
        self._active_count = 0
        self._schedule_lock = asyncio.Lock()

    async def spawn_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request = SpawnAgentRequest.model_validate(payload)
            cwd, external = normalize_roots(request.cwd, request.allowed_external_roots)
            items_error = self._validate_items(request.items, cwd, external, request.explicit_authorizations)
            if items_error:
                return items_error
        except ValidationError as exc:
            return err(ErrorCode.invalid_request, "invalid spawn request", _validation_error_details(exc))
        except SecurityError as exc:
            return err(exc.error.code, exc.error.message, exc.error.details)
        except Exception as exc:
            return err(ErrorCode.invalid_request, str(exc))

        agent_id = f"run_{uuid.uuid4().hex}"
        run_dir = self.store.run_dir(agent_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        now = utc_now()
        state = RunState(
            agent_id=agent_id,
            status=AgentStatus.created,
            agent_type=request.agent_type,
            cwd=str(cwd),
            run_dir=str(run_dir),
            current_segment_id="seg_0001",
            model=request.model or self.settings.openai_model_name,
            dry_run=request.dry_run,
            max_steps=request.max_steps,
            timeout_seconds=request.timeout_seconds,
            allowed_external_roots=[str(p) for p in external],
            explicit_authorizations=request.explicit_authorizations,
            created_at=now,
            updated_at=now,
            queued_inputs=[],
        )
        write_json(run_dir / "request.json", request.model_dump(mode="json"))
        self.store.save_state(state)
        self._record_items(agent_id, "seg_0001", request.items)
        item_catalog = self._item_catalog(agent_id)
        state.queued_inputs = [
            {
                "message": request.message,
                "items": request.items,
                "item_catalog": item_catalog,
                "segment_id": "seg_0001",
            }
        ]
        self.store.save_state(state)
        self.store.append_event(
            agent_id,
            "spawn_agent",
            {
                "agent_type": request.agent_type.value,
                "cwd": str(cwd),
                "segment_id": "seg_0001",
                "item_count": len(item_catalog),
            },
        )
        self.store.append_segment(state, SegmentRecord(segment_id="seg_0001", reason="spawn_agent", started_at=now))
        await self._schedule_or_queue(state)
        state = self.store.load_state(agent_id)
        return ok(
            {
                "agent_id": agent_id,
                "status": state.status.value,
                "agent_type": state.agent_type.value,
                "run_dir": str(run_dir),
                "cwd": str(cwd),
                "current_segment_id": state.current_segment_id,
            }
        )

    def _record_items(self, agent_id: str, segment_id: str, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        items_path = self.store.run_dir(agent_id) / "items.jsonl"
        existing_count = len(read_jsonl(items_path))
        for offset, item in enumerate(items, start=1):
            item_id = f"item_{existing_count + offset:04d}"
            item_type = item.get("type")
            if item_type == "text":
                parsed = TextItem.model_validate(item)
                preview, preview_truncated = truncate_text(redact_text(parsed.text), 500)
                record = {
                    "item_id": item_id,
                    "segment_id": segment_id,
                    "type": "text",
                    "name": parsed.name,
                    "size": len(parsed.text),
                    "preview": preview,
                    "preview_truncated": preview_truncated,
                    "text": parsed.text,
                    "timestamp": utc_now(),
                }
            elif item_type == "local_path":
                parsed = LocalPathItem.model_validate(item)
                record = {
                    "item_id": item_id,
                    "segment_id": segment_id,
                    "type": "local_path",
                    "name": parsed.name,
                    "path": parsed.path,
                    "size": None,
                    "preview": parsed.path,
                    "preview_truncated": False,
                    "timestamp": utc_now(),
                }
            else:
                continue
            append_jsonl(items_path, record)

    def _item_catalog(self, agent_id: str) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for row in read_jsonl(self.store.run_dir(agent_id) / "items.jsonl"):
            item = {
                "item_id": row.get("item_id"),
                "segment_id": row.get("segment_id"),
                "type": row.get("type"),
                "name": row.get("name"),
                "size": row.get("size"),
                "preview": row.get("preview"),
                "preview_truncated": row.get("preview_truncated"),
            }
            if row.get("type") == "local_path":
                item["path"] = row.get("path")
            catalog.append(item)
        return catalog

    def _validate_items(
        self,
        items: list[dict[str, Any]],
        cwd: Path,
        external: list[Path],
        authorizations: list[Authorization],
    ) -> dict[str, Any] | None:
        for item in items:
            item_type = item.get("type")
            try:
                if item_type == "text":
                    TextItem.model_validate(item)
                elif item_type == "local_path":
                    parsed = LocalPathItem.model_validate(item)
                    resolve_path(
                        parsed.path,
                        cwd,
                        external,
                        operation="read",
                        explicit_authorizations=authorizations,
                        sensitive_patterns=self.settings.sensitive_path_patterns,
                    )
                else:
                    return err(ErrorCode.unsupported_item_type, f"unsupported item type {item_type}")
            except ValidationError as exc:
                return err(ErrorCode.invalid_request, "invalid item", _validation_error_details(exc))
            except SecurityError as exc:
                return err(exc.error.code, exc.error.message, exc.error.details)
        return None

    async def _schedule_or_queue(self, state: RunState) -> None:
        async with self._schedule_lock:
            if self._active_count >= self.settings.max_concurrency:
                state.status = AgentStatus.queued
                self.store.save_state(state)
                self._pending.append(state.agent_id)
                return
            self._start_task_locked(state)

    def _start_task_locked(self, state: RunState) -> None:
        state.status = AgentStatus.running
        self.store.save_state(state)
        self.store.append_event(state.agent_id, "running", {"segment_id": state.current_segment_id})
        self._active_count += 1
        task = asyncio.create_task(self._run_agent(state.agent_id))
        self._tasks[state.agent_id] = task
        task.add_done_callback(lambda _: asyncio.create_task(self._on_task_done(state.agent_id)))

    async def _on_task_done(self, agent_id: str) -> None:
        async with self._schedule_lock:
            self._tasks.pop(agent_id, None)
            self._active_count = max(0, self._active_count - 1)
            while self._pending:
                next_id = self._pending.pop(0)
                try:
                    state = self.store.load_state(next_id)
                except KeyError:
                    continue
                if state.status != AgentStatus.queued:
                    continue
                self._start_task_locked(state)
                break

    async def _run_agent(self, agent_id: str) -> None:
        state = self.store.load_state(agent_id)
        run_dir = Path(state.run_dir)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]
        while state.step_count < state.max_steps:
            if state.status == AgentStatus.closing:
                break
            queued = state.queued_inputs.pop(0) if state.queued_inputs else None
            if queued:
                state.current_segment_id = queued.get("segment_id", state.current_segment_id)
                messages.append(
                    {
                        "role": "user",
                        "content": build_task_package(
                            agent_id=state.agent_id,
                            agent_type=state.agent_type.value,
                            cwd=state.cwd,
                            message=queued["message"],
                            current_segment_id=state.current_segment_id,
                            authorizations=[a.value for a in state.explicit_authorizations],
                            dry_run=state.dry_run,
                            item_catalog=queued.get("item_catalog", []),
                        ),
                    }
                )
                self.store.save_state(state)
            try:
                result = await self._chat_with_length_retry(state, messages)
            except LLMError as exc:
                self._fail_run(
                    state,
                    reason=exc.code.value,
                    message=str(exc),
                    details=exc.details,
                )
                append_jsonl(run_dir / "actions.jsonl", {"error": exc.code.value, "details": exc.details})
                return

            self._record_model_output(state, result)
            try:
                parsed = parse_action(result.content)
                state.format_error_count = 0
                state.last_error = None
            except ActionParseError as exc:
                state.format_error_count += 1
                self._record_parse_error(state, exc, result.content)
                self.store.save_state(state)
                append_jsonl(
                    run_dir / "actions.jsonl",
                    {"ok": False, "code": exc.code.value, "message": str(exc), "details": exc.details},
                )
                if state.format_error_count >= 3:
                    self._fail_with_parse_fallback(state, result.content, exc)
                    return
                repair_error = str(exc)
                if exc.details:
                    repair_error = json.dumps(exc.details, ensure_ascii=False)
                messages.append({"role": "user", "content": build_repair_prompt(result.content, repair_error)})
                continue

            state.step_count += 1
            self.store.save_state(state)
            observation = await self.executor.execute(state, parsed)
            self.store.append_event(
                state.agent_id,
                "action",
                {"action_id": parsed.action_id, "action": parsed.action, "ok": observation.ok},
            )
            append_jsonl(
                run_dir / "actions.jsonl",
                {
                    "action_id": parsed.action_id,
                    "action": parsed.action,
                    "args": parsed.args,
                    "observation": observation.model_dump(mode="json"),
                    "timestamp": utc_now(),
                },
            )
            messages.append({"role": "user", "content": observation.model_dump_json()})
            if parsed.action == "request_main_tool" and observation.ok:
                await self._wait_for_main_tool(state, parsed.args)
                return
            if parsed.action == "finish" and observation.ok:
                await self._finish(state, parsed.args)
                return
            state = self.store.load_state(agent_id)
        state.status = AgentStatus.failed
        self._fail_run(
            state,
            reason="max_steps_exceeded",
            message="Open Subagent MCP exceeded max_steps before producing a finish action.",
            details={"step_count": state.step_count, "max_steps": state.max_steps},
        )

    def _record_model_output(self, state: RunState, result: Any) -> None:
        run_dir = Path(state.run_dir)
        timestamp = utc_now()
        raw_output_path = run_dir / "raw_model_output.txt"
        raw_output_path.parent.mkdir(parents=True, exist_ok=True)
        with raw_output_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n\n--- model output {timestamp} ---\n")
            handle.write(result.content)
        preview, preview_truncated = truncate_text(redact_text(result.content), MODEL_OUTPUT_PREVIEW_LIMIT)
        event = {
            "timestamp": timestamp,
            "finish_reason": result.finish_reason,
            "raw_output_path": str(raw_output_path),
            "content_preview": preview,
            "preview_truncated": preview_truncated,
        }
        append_jsonl(run_dir / "messages.jsonl", {"role": "assistant", "content": result.content, "raw": result.raw})
        append_jsonl(run_dir / "raw_model_outputs.jsonl", {"content": result.content, "timestamp": timestamp})
        state.raw_output_path = str(raw_output_path)
        state.last_model_event = event
        self.store.save_state(state)
        self.store.append_event(state.agent_id, "model_output", event)

    def _record_parse_error(self, state: RunState, exc: ActionParseError, raw_output: str) -> None:
        run_dir = Path(state.run_dir)
        preview, preview_truncated = truncate_text(redact_text(raw_output), MODEL_OUTPUT_PREVIEW_LIMIT)
        payload = {
            "run_id": state.agent_id,
            "code": exc.code.value,
            "message": str(exc),
            "details": exc.details,
            "format_error_count": state.format_error_count,
            "raw_output_path": state.raw_output_path,
            "raw_preview": preview,
            "raw_preview_truncated": preview_truncated,
            "timestamp": utc_now(),
        }
        parse_error_path = run_dir / "parse_error.json"
        write_json(parse_error_path, payload)
        append_jsonl(run_dir / "parse_errors.jsonl", payload)
        state.parse_error_path = str(parse_error_path)
        state.last_error = payload
        self.store.append_event(
            state.agent_id,
            "parse_error",
            {
                "code": exc.code.value,
                "message": str(exc),
                "format_error_count": state.format_error_count,
                "parse_error_path": str(parse_error_path),
            },
        )

    def _write_final_message(self, state: RunState) -> str:
        path = Path(state.run_dir) / "final_message.md"
        path.write_text(state.final_message or "", encoding="utf-8")
        return str(path)

    def _write_failure_result(
        self,
        state: RunState,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        result_path = Path(state.run_dir) / "result.json"
        if result_path.exists():
            return
        write_json(
            result_path,
            {
                "status": state.status.value,
                "summary": state.final_message,
                "failure_reason": reason,
                "message": message,
                "details": details or {},
                "parse_warning": state.parse_warning,
                "raw_output_path": state.raw_output_path,
                "parse_error_path": state.parse_error_path,
                "error_path": state.error_path,
                "tests": [],
                "risk_notes": [],
                "open_issues": [],
            },
        )

    def _fail_run(
        self,
        state: RunState,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None = None,
        status: AgentStatus = AgentStatus.failed,
    ) -> None:
        state.status = status
        state.failure_reason = reason
        state.ended_at = utc_now()
        if not state.final_message:
            fallback, _ = truncate_text(f"{reason}: {message}", FINAL_MESSAGE_LIMIT)
            state.final_message = fallback
        self.store.write_error(state, reason=reason, message=message, details=details)
        final_message_path = self._write_final_message(state)
        self._write_failure_result(state, reason=reason, message=message, details=details)
        self.store.save_state(state)
        self.store.append_event(
            state.agent_id,
            "failed" if status == AgentStatus.failed else status.value,
            {
                "reason": reason,
                "error_path": state.error_path,
                "final_message_path": final_message_path,
            },
        )

    def _fail_with_parse_fallback(self, state: RunState, raw_output: str, exc: ActionParseError) -> None:
        raw_text = raw_output.strip() or f"{exc.code.value}: {exc}"
        summary, truncated = truncate_text(raw_text, FINAL_MESSAGE_LIMIT)
        state.final_message = summary
        state.parse_warning = "structured action parsing failed; raw model output returned as final_message"
        write_json(
            Path(state.run_dir) / "result.json",
            {
                "status": AgentStatus.failed.value,
                "summary": summary,
                "failure_reason": exc.code.value,
                "parse_warning": state.parse_warning,
                "parse_error": str(exc),
                "parse_error_details": exc.details,
                "raw_output_path": state.raw_output_path,
                "parse_error_path": state.parse_error_path,
                "raw_output_truncated": truncated,
                "tests": [],
                "risk_notes": [],
                "open_issues": [],
            },
        )
        self._fail_run(
            state,
            reason=exc.code.value,
            message=str(exc),
            details={"parse_error_path": state.parse_error_path, "raw_output_path": state.raw_output_path},
        )

    async def _chat_with_length_retry(self, state: RunState, messages: list[dict[str, str]]):
        try:
            return await self.llm_client.chat(model=state.model, messages=messages, temperature=0.1)
        except LLMError as exc:
            if exc.code == ErrorCode.llm_truncated:
                return await self.llm_client.chat(model=state.model, messages=messages, temperature=0.1)
            raise

    async def _finish(self, state: RunState, finish_args: dict[str, Any]) -> None:
        run_dir = Path(state.run_dir)
        status = finish_args["status"]
        if status == "completed":
            state.status = AgentStatus.completed
        elif status == "waiting_input":
            state.status = AgentStatus.waiting_input
        else:
            state.status = AgentStatus.failed
        state.final_message = finish_args["summary"]
        state.ended_at = utc_now()
        write_json(run_dir / "result.json", finish_args)
        (run_dir / "final_message.md").write_text(state.final_message, encoding="utf-8")
        diff = git_diff(Path(state.cwd))
        (run_dir / "changes.diff").write_text(diff, encoding="utf-8")
        self.store.save_state(state)
        self.store.append_event(
            state.agent_id,
            "finish",
            {"status": state.status.value, "final_message_path": str(run_dir / "final_message.md")},
        )

    async def _wait_for_main_tool(self, state: RunState, request_args: dict[str, Any]) -> None:
        run_dir = Path(state.run_dir)
        state.status = AgentStatus.waiting_input
        state.final_message = f"Waiting for the MCP host to provide {request_args.get('tool', 'requested tool')} result."
        state.ended_at = utc_now()
        requested = {
            "agent_id": state.agent_id,
            "segment_id": state.current_segment_id,
            "requested_main_tool": request_args,
            "timestamp": utc_now(),
        }
        write_json(run_dir / "requested_main_tool.json", requested)
        write_json(
            run_dir / "result.json",
            {
                "status": "waiting_input",
                "summary": state.final_message,
                "requested_main_tool": request_args,
                "tests": [],
                "risk_notes": ["waiting for MCP host/orchestrator tool broker"],
                "open_issues": ["The MCP host or orchestrator must call or decline the requested tool and send the result back."],
            },
        )
        (run_dir / "final_message.md").write_text(state.final_message, encoding="utf-8")
        self.store.save_state(state)
        self.store.append_event(
            state.agent_id,
            "request_main_tool",
            {"requested_main_tool_path": str(run_dir / "requested_main_tool.json"), "tool": request_args.get("tool")},
        )

    def _recover_unowned_transient_state(self, state: RunState) -> RunState:
        if state.status not in TRANSIENT_STATUSES:
            return state
        if state.status == AgentStatus.waiting_input:
            return state
        if state.status == AgentStatus.queued and state.agent_id in self._pending:
            return state
        if state.status == AgentStatus.running and state.agent_id in self._tasks:
            return state
        if state.status == AgentStatus.closing and (
            state.agent_id in self._tasks or state.agent_id in self.active_processes
        ):
            return state
        previous_status = state.status.value
        self._fail_run(
            state,
            reason="interrupted_after_restart",
            message="Open Subagent MCP has persisted run state but no active runtime task owns it.",
            details={"previous_status": previous_status},
            status=AgentStatus.interrupted,
        )
        self.store.append_event(
            state.agent_id,
            "interrupted_runtime_missing",
            {"previous_status": previous_status},
        )
        return self.store.load_state(state.agent_id)

    async def wait_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        targets = payload.get("targets") or []
        timeout_ms = int(payload.get("timeout_ms", 30000))
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while True:
            states = []
            missing = []
            for target in targets:
                try:
                    states.append(self._recover_unowned_transient_state(self.store.load_state(target)))
                except KeyError:
                    missing.append(target)
            if missing:
                return err(ErrorCode.unknown_agent, "unknown agent", {"targets": missing})
            if all(state.status in TERMINAL_STATUSES or state.status == AgentStatus.waiting_input for state in states):
                break
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.05)
        completed: dict[str, Any] = {}
        timed_out: list[str] = []
        for state in states:
            if state.status in TERMINAL_STATUSES or state.status == AgentStatus.waiting_input:
                completed[state.agent_id] = self._result_for_state(state)
            else:
                timed_out.append(state.agent_id)
        return ok({"completed": completed, "timed_out": timed_out})

    def _result_for_state(self, state: RunState) -> dict[str, Any]:
        run_dir = Path(state.run_dir)
        command_effects = read_jsonl(run_dir / "command_effects.jsonl")
        commands = read_jsonl(run_dir / "commands.jsonl")
        writes = read_jsonl(run_dir / "writes.jsonl")
        result_path = run_dir / "result.json"
        result = read_json(result_path) if result_path.exists() else {}
        final_message_path = run_dir / "final_message.md"
        events_path = run_dir / "events.jsonl"
        status_path = run_dir / "status.json"
        items_path = run_dir / "items.jsonl"
        requested_main_tool_path = run_dir / "requested_main_tool.json"
        response = {
            "status": state.status.value,
            "final_message": state.final_message,
            "failure_reason": state.failure_reason,
            "parse_warning": state.parse_warning,
            "result": result,
            "run_dir": state.run_dir,
            "status_path": str(status_path) if status_path.exists() else None,
            "events_path": str(events_path) if events_path.exists() else None,
            "raw_output_path": state.raw_output_path,
            "parse_error_path": state.parse_error_path,
            "error_path": state.error_path,
            "final_message_path": str(final_message_path) if final_message_path.exists() else None,
            "items_path": str(items_path) if items_path.exists() else None,
            "requested_main_tool": read_json(requested_main_tool_path).get("requested_main_tool") if requested_main_tool_path.exists() else None,
            "current_segment_id": state.current_segment_id,
            "last_error": state.last_error,
            "last_model_event": state.last_model_event,
            "changed_files": sorted({row.get("path") for row in writes + command_effects if row.get("path")}),
            "diff_path": str(run_dir / "changes.diff") if (run_dir / "changes.diff").exists() else None,
            "commands_run": commands,
            "command_effects": command_effects,
            "rollback_segments": self.store.list_segments(state.agent_id),
            "tests": result.get("tests", []),
            "risk_notes": result.get("risk_notes", []),
        }
        diagnostics = self._diagnostics_for_state(state, run_dir)
        if diagnostics:
            response["diagnostics"] = diagnostics
        return response

    def _diagnostics_for_state(self, state: RunState, run_dir: Path) -> dict[str, Any] | None:
        if state.failure_reason != "max_steps_exceeded":
            return None
        last_action: dict[str, Any] | None = None
        for row in read_jsonl(run_dir / "actions.jsonl"):
            if row.get("action"):
                last_action = {"action": row.get("action"), "args": row.get("args", {})}
        return {
            "step_count": state.step_count,
            "max_steps": state.max_steps,
            "last_action": last_action,
            "suggestion": (
                "Increase max_steps or narrow the task/search scope. Avoid broad repository scans, "
                ".conda, .venv, node_modules, caches, and large data files."
            ),
        }

    async def send_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = payload.get("target")
        if not target or not self.store.exists(target):
            return err(ErrorCode.unknown_agent, "unknown agent")
        state = self.store.load_state(target)
        if state.status in {
            AgentStatus.closed,
            AgentStatus.rolled_back,
            AgentStatus.partially_rolled_back,
            AgentStatus.failed,
            AgentStatus.crashed,
            AgentStatus.interrupted,
        }:
            return err(ErrorCode.invalid_state, "agent cannot receive input in this state", {"status": state.status.value})
        if (
            state.status in TRANSIENT_STATUSES
            and target not in self._tasks
            and state.status not in {AgentStatus.queued, AgentStatus.waiting_input}
        ):
            return err(ErrorCode.invalid_state, "agent task registry is missing")
        message = payload.get("message") or ""
        if not message.strip():
            return err(ErrorCode.invalid_request, "message is required")
        items = payload.get("items", [])
        try:
            cwd = Path(state.cwd).resolve()
            external = [Path(p).resolve() for p in state.allowed_external_roots]
            items_error = self._validate_items(items, cwd, external, state.explicit_authorizations)
            if items_error:
                return items_error
        except SecurityError as exc:
            return err(exc.error.code, exc.error.message, exc.error.details)
        if state.status == AgentStatus.queued:
            self._record_items(target, state.current_segment_id, items)
            item_catalog = self._item_catalog(target)
            state.queued_inputs.append(
                {"message": message, "items": items, "item_catalog": item_catalog, "segment_id": state.current_segment_id}
            )
            self.store.save_state(state)
            return ok({"agent_id": target, "status": state.status.value, "current_segment_id": state.current_segment_id})
        next_segment = self._next_segment_id(target)
        self.store.append_segment(state, SegmentRecord(segment_id=next_segment, reason="send_input", started_at=utc_now()))
        state = self.store.load_state(target)
        self._record_items(target, next_segment, items)
        item_catalog = self._item_catalog(target)
        state.queued_inputs.append({"message": message, "items": items, "item_catalog": item_catalog, "segment_id": next_segment})
        state.ended_at = None
        if state.status in {AgentStatus.completed, AgentStatus.waiting_input}:
            await self._schedule_or_queue(state)
        else:
            self.store.save_state(state)
        state = self.store.load_state(target)
        return ok({"agent_id": target, "status": state.status.value, "current_segment_id": state.current_segment_id})

    def _next_segment_id(self, agent_id: str) -> str:
        segments = self.store.list_segments(agent_id)
        return f"seg_{len(segments) + 1:04d}"

    async def close_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = payload.get("target")
        if not target or not self.store.exists(target):
            return err(ErrorCode.unknown_agent, "unknown agent")
        state = self.store.load_state(target)
        previous = state.status
        state.status = AgentStatus.closing
        self.store.save_state(state)
        process = self.active_processes.get(target)
        if process is not None:
            from .actions import terminate_process_group

            await terminate_process_group(process)
        task = self._tasks.get(target)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        state = self.store.load_state(target)
        state.status = AgentStatus.closed
        state.ended_at = utc_now()
        self.store.save_state(state)
        return ok({"agent_id": target, "previous_status": previous.value, "status": "closed"})

    async def rollback_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = payload.get("agent_id")
        if not agent_id or not self.store.exists(agent_id):
            return err(ErrorCode.unknown_agent, "unknown agent")
        state = self.store.load_state(agent_id)
        try:
            result = rollback_run(
                state=state,
                segment_id=payload.get("segment_id"),
                paths=payload.get("paths", []),
                include_command_effects=payload.get("include_command_effects", True),
                force=payload.get("force", False),
            )
            return ok(result)
        except Exception as exc:
            return err(ErrorCode.internal_error, str(exc))
