from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_subagent_mcp.config import Settings
from open_subagent_mcp.llm_client import FakeLLMClient, LLMError
from open_subagent_mcp.models import AgentStatus, ErrorCode
from open_subagent_mcp.runtime import OpenSubagentRuntime
from open_subagent_mcp.workspace import utc_now, write_json


def finish(summary: str = "done") -> str:
    return json.dumps(
        {
            "action": "finish",
            "args": {
                "status": "completed",
                "summary": summary,
                "self_check_commands": ["manual review"],
                "tests": [],
                "risk_notes": ["fake test"],
                "open_issues": [],
            },
        }
    )


@pytest.mark.asyncio
async def test_completed_send_input_creates_new_segment(tmp_path: Path) -> None:
    settings = Settings(runs_dir=tmp_path / ".runs")
    fake = FakeLLMClient([finish("first"), finish("second")])
    runtime = OpenSubagentRuntime(settings=settings, llm_client=fake)
    response = await runtime.spawn_agent({"agent_type": "worker", "message": "do x", "cwd": str(tmp_path)})
    agent_id = response["data"]["agent_id"]
    await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 2000})
    sent = await runtime.send_input({"target": agent_id, "message": "continue"})
    assert sent["data"]["current_segment_id"] == "seg_0002"
    await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 2000})
    segments = runtime.store.list_segments(agent_id)
    assert [s["segment_id"] for s in segments] == ["seg_0001", "seg_0002"]


@pytest.mark.asyncio
async def test_waiting_input_send_input_restarts_without_task_registry(tmp_path: Path) -> None:
    settings = Settings(runs_dir=tmp_path / ".runs")
    fake = FakeLLMClient(
        [
            json.dumps(
                {
                    "action": "finish",
                    "args": {
                        "status": "waiting_input",
                        "summary": "need input",
                        "self_check_commands": [],
                        "tests": [],
                        "risk_notes": ["waiting for user"],
                        "open_issues": [],
                    },
                }
            ),
            finish("resumed"),
        ]
    )
    runtime = OpenSubagentRuntime(settings=settings, llm_client=fake)
    response = await runtime.spawn_agent({"agent_type": "worker", "message": "ask", "cwd": str(tmp_path)})
    agent_id = response["data"]["agent_id"]
    waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 2000})
    assert waited["data"]["completed"][agent_id]["status"] == "waiting_input"
    sent = await runtime.send_input({"target": agent_id, "message": "continue"})
    assert sent["ok"]
    assert sent["data"]["current_segment_id"] == "seg_0002"
    waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 2000})
    assert waited["data"]["completed"][agent_id]["status"] == "completed"


@pytest.mark.asyncio
async def test_close_blocks_future_send_input(tmp_path: Path) -> None:
    settings = Settings(runs_dir=tmp_path / ".runs")
    runtime = OpenSubagentRuntime(settings=settings, llm_client=FakeLLMClient([finish()]))
    response = await runtime.spawn_agent({"agent_type": "worker", "message": "do x", "cwd": str(tmp_path)})
    agent_id = response["data"]["agent_id"]
    await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 2000})
    closed = await runtime.close_agent({"target": agent_id})
    assert closed["data"]["status"] == "closed"
    sent = await runtime.send_input({"target": agent_id, "message": "again"})
    assert not sent["ok"]


def test_restart_marks_orphan_running_interrupted(tmp_path: Path) -> None:
    runs_dir = tmp_path / ".runs"
    run_dir = runs_dir / "run_orphan"
    run_dir.mkdir(parents=True)
    now = utc_now()
    write_json(
        run_dir / "state.json",
        {
            "agent_id": "run_orphan",
            "status": "running",
            "agent_type": "worker",
            "cwd": str(tmp_path),
            "run_dir": str(run_dir),
            "current_segment_id": "seg_0001",
            "model": "fake",
            "created_at": now,
            "updated_at": now,
        },
    )
    runtime = OpenSubagentRuntime(settings=Settings(runs_dir=runs_dir), llm_client=FakeLLMClient([]))
    state = runtime.store.load_state("run_orphan")
    assert state.status == AgentStatus.interrupted
    assert state.failure_reason == "interrupted_after_restart"
    assert (run_dir / "status.json").exists()
    assert (run_dir / "events.jsonl").exists()
    assert (run_dir / "error.json").exists()


@pytest.mark.asyncio
async def test_interrupted_orphan_cannot_be_restarted_by_send_input(tmp_path: Path) -> None:
    runs_dir = tmp_path / ".runs"
    run_dir = runs_dir / "run_orphan"
    run_dir.mkdir(parents=True)
    now = utc_now()
    write_json(
        run_dir / "state.json",
        {
            "agent_id": "run_orphan",
            "status": "running",
            "agent_type": "worker",
            "cwd": str(tmp_path),
            "run_dir": str(run_dir),
            "current_segment_id": "seg_0001",
            "model": "fake",
            "created_at": now,
            "updated_at": now,
        },
    )
    runtime = OpenSubagentRuntime(settings=Settings(runs_dir=runs_dir), llm_client=FakeLLMClient([finish()]))
    response = await runtime.send_input({"target": "run_orphan", "message": "resume"})
    assert not response["ok"]
    assert response["error"]["code"] == "invalid_state"


@pytest.mark.asyncio
async def test_wait_agent_recovers_unowned_running_state_from_disk(tmp_path: Path) -> None:
    settings = Settings(runs_dir=tmp_path / ".runs")
    runtime = OpenSubagentRuntime(settings=settings, llm_client=FakeLLMClient([]))
    run_dir = settings.runs_dir / "run_lost"
    run_dir.mkdir(parents=True)
    now = utc_now()
    write_json(
        run_dir / "state.json",
        {
            "agent_id": "run_lost",
            "status": "running",
            "agent_type": "worker",
            "cwd": str(tmp_path),
            "run_dir": str(run_dir),
            "current_segment_id": "seg_0001",
            "model": "fake",
            "created_at": now,
            "updated_at": now,
        },
    )
    waited = await runtime.wait_agent({"targets": ["run_lost"], "timeout_ms": 10})
    result = waited["data"]["completed"]["run_lost"]
    assert result["status"] == "interrupted"
    assert result["failure_reason"] == "interrupted_after_restart"
    assert result["run_dir"] == str(run_dir)
    assert result["status_path"] == str(run_dir / "status.json")
    assert result["events_path"] == str(run_dir / "events.jsonl")
    assert result["error_path"] == str(run_dir / "error.json")


@pytest.mark.asyncio
async def test_max_steps_failure_returns_diagnostics(tmp_path: Path) -> None:
    settings = Settings(runs_dir=tmp_path / ".runs")
    runtime = OpenSubagentRuntime(
        settings=settings,
        llm_client=FakeLLMClient(
            [
                json.dumps({"action": "read_file", "args": {"path": "note.txt"}}),
                json.dumps({"action": "read_file", "args": {"path": "note.txt"}}),
            ]
        ),
    )
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    response = await runtime.spawn_agent(
        {"agent_type": "explorer", "message": "keep reading", "cwd": str(tmp_path), "max_steps": 2}
    )
    agent_id = response["data"]["agent_id"]
    waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 2000})
    result = waited["data"]["completed"][agent_id]
    assert result["status"] == "failed"
    assert result["failure_reason"] == "max_steps_exceeded"
    assert result["diagnostics"]["step_count"] == 2
    assert result["diagnostics"]["max_steps"] == 2
    assert result["diagnostics"]["last_action"]["action"] == "read_file"
    assert "Increase max_steps" in result["diagnostics"]["suggestion"]
    assert result["final_message"]
    assert result["error_path"]
    assert result["final_message_path"]


@pytest.mark.asyncio
async def test_parse_failure_returns_raw_output_and_debug_paths(tmp_path: Path) -> None:
    settings = Settings(runs_dir=tmp_path / ".runs")
    raw = "I finished the analysis, but this is not a JSON action."
    runtime = OpenSubagentRuntime(
        settings=settings,
        llm_client=FakeLLMClient([raw, raw, raw]),
    )
    response = await runtime.spawn_agent({"agent_type": "explorer", "message": "summarize", "cwd": str(tmp_path)})
    agent_id = response["data"]["agent_id"]
    waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 2000})
    result = waited["data"]["completed"][agent_id]
    run_dir = Path(result["run_dir"])
    assert result["status"] == "failed"
    assert result["failure_reason"] == "model_output_parse_error"
    assert result["final_message"] == raw
    assert result["parse_warning"] == "structured action parsing failed; raw model output returned as final_message"
    assert result["raw_output_path"] == str(run_dir / "raw_model_output.txt")
    assert result["parse_error_path"] == str(run_dir / "parse_error.json")
    assert result["error_path"] == str(run_dir / "error.json")
    assert result["final_message_path"] == str(run_dir / "final_message.md")
    assert result["last_model_event"]["raw_output_path"] == str(run_dir / "raw_model_output.txt")
    assert (run_dir / "raw_model_output.txt").read_text(encoding="utf-8").count(raw) == 3
    assert json.loads((run_dir / "result.json").read_text(encoding="utf-8"))["parse_warning"]


@pytest.mark.asyncio
async def test_llm_http_error_returns_debug_paths_and_attempts(tmp_path: Path) -> None:
    settings = Settings(runs_dir=tmp_path / ".runs")
    runtime = OpenSubagentRuntime(
        settings=settings,
        llm_client=FakeLLMClient(
            [
                LLMError(
                    ErrorCode.llm_http_error,
                    "LLM HTTP 502 after 3 attempt(s)",
                    {
                        "status_code": 502,
                        "attempts": [{"attempt": 1, "status_code": 502}],
                        "max_attempts": 3,
                    },
                )
            ]
        ),
    )
    response = await runtime.spawn_agent({"agent_type": "explorer", "message": "read", "cwd": str(tmp_path)})
    agent_id = response["data"]["agent_id"]
    waited = await runtime.wait_agent({"targets": [agent_id], "timeout_ms": 2000})
    result = waited["data"]["completed"][agent_id]
    run_dir = Path(result["run_dir"])
    assert result["status"] == "failed"
    assert result["failure_reason"] == "llm_http_error"
    assert result["final_message"] == "llm_http_error: LLM HTTP 502 after 3 attempt(s)"
    assert result["error_path"] == str(run_dir / "error.json")
    assert result["final_message_path"] == str(run_dir / "final_message.md")
    assert result["last_error"]["details"]["status_code"] == 502
    assert result["last_error"]["details"]["attempts"][0]["status_code"] == 502


@pytest.mark.asyncio
async def test_concurrency_queues_extra_runs(tmp_path: Path, python_cmd: str) -> None:
    settings = Settings(runs_dir=tmp_path / ".runs", max_concurrency=1)
    slow = json.dumps({"action": "run_command", "args": {"cmd": f"{python_cmd} -c \"import time; time.sleep(0.3)\"", "reason": "sleep"}})
    runtime = OpenSubagentRuntime(settings=settings, llm_client=FakeLLMClient([slow, finish("first"), finish("second")]))
    first = await runtime.spawn_agent({"agent_type": "worker", "message": "first", "cwd": str(tmp_path)})
    second = await runtime.spawn_agent({"agent_type": "worker", "message": "second", "cwd": str(tmp_path)})
    assert first["data"]["status"] == "running"
    assert second["data"]["status"] == "queued"
    await runtime.wait_agent({"targets": [first["data"]["agent_id"], second["data"]["agent_id"]], "timeout_ms": 5000})
