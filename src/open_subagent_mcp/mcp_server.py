from __future__ import annotations

import json
import os
from typing import Any

from .llm_client import FakeLLMClient
from .runtime import OpenSubagentRuntime

SERVER_INSTRUCTIONS = """Open Subagent MCP is a local stdio MCP server for developer workstations.
It delegates subagent model calls to the configured OpenAI-compatible chat
completions endpoint and stores run state, logs, snapshots, and rollback
metadata locally under SUBAGENT_MCP_RUNS_DIR or .runs.

The runtime enforces cwd and allowed_external_roots path bounds, blocks sensitive
paths such as .env, .ssh, keys, tokens, and credentials, records writes and
command effects, and supports best-effort local file rollback by run or segment.
Do not use this server to read credentials, secrets, production data, or
unauthorized external paths.

The MCP host or orchestrator remains responsible for reviewing subagent output
before presenting it as a final user result. Open Subagent MCP uses structured
JSON actions, reads injected context before specialized work, and uses
request_main_tool when it needs the host or orchestrator to perform capabilities
that are not available inside this runtime."""


def _build_runtime() -> OpenSubagentRuntime:
    fake_outputs = os.getenv("SUBAGENT_MCP_FAKE_LLM_OUTPUTS")
    if fake_outputs:
        return OpenSubagentRuntime(llm_client=FakeLLMClient(json.loads(fake_outputs)))
    return OpenSubagentRuntime()


runtime = _build_runtime()


def create_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:  # pragma: no cover - exercised when dependency missing
        raise RuntimeError("mcp package is required to run the stdio server") from exc

    server = FastMCP("open_subagent_mcp", instructions=SERVER_INSTRUCTIONS)

    @server.tool()
    async def subagent_spawn(
        agent_type: str,
        message: str,
        cwd: str,
        items: list[dict[str, Any]] | None = None,
        fork_context: bool = False,
        model: str | None = None,
        dry_run: bool = False,
        max_steps: int = 160,
        timeout_seconds: int = 120,
        allowed_external_roots: list[str] | None = None,
        explicit_authorizations: list[str] | None = None,
    ) -> dict[str, Any]:
        """Start an Open Subagent MCP run.

        agent_type must be "explorer" for read-only exploration or "worker" for
        writable work. By default, Open Subagent MCP may only access files under cwd.
        Repository-external paths must be declared in allowed_external_roots and
        still pass realpath, symlink, and sensitive path checks.

        Keep timeout_seconds <= 120 for normal tasks. For larger tasks, increase
        max_steps first and narrow the search scope. If timeout_seconds > 120 is
        required, pass explicit_authorizations=["long_running_commands"].
        """
        return await runtime.spawn_agent(
            {
                "agent_type": agent_type,
                "message": message,
                "cwd": cwd,
                "items": items or [],
                "fork_context": fork_context,
                "model": model,
                "dry_run": dry_run,
                "max_steps": max_steps,
                "timeout_seconds": timeout_seconds,
                "allowed_external_roots": allowed_external_roots or [],
                "explicit_authorizations": explicit_authorizations or [],
            }
        )

    @server.tool()
    async def subagent_wait(targets: list[str], timeout_ms: int = 30000) -> dict[str, Any]:
        """Wait for one or more Open Subagent MCP runs.

        Results include status, summaries, changed_files, commands_run,
        command_effects, and rollback_segments so the MCP host can audit
        what the subagent did before using its answer.
        """
        return await runtime.wait_agent({"targets": targets, "timeout_ms": timeout_ms})

    @server.tool()
    async def subagent_send_message(
        target: str,
        message: str,
        items: list[dict[str, Any]] | None = None,
        interrupt: bool = False,
    ) -> dict[str, Any]:
        """Send a follow-up message to an existing Open Subagent MCP run.

        Each follow-up creates a new rollback segment. Use segment_id with
        subagent_rollback to undo only the follow-up's effects.
        """
        return await runtime.send_input(
            {"target": target, "message": message, "items": items or [], "interrupt": interrupt}
        )

    @server.tool()
    async def subagent_close(target: str) -> dict[str, Any]:
        """Close an Open Subagent MCP run and release runtime state."""
        return await runtime.close_agent({"target": target})

    @server.tool()
    async def subagent_rollback(
        agent_id: str,
        segment_id: str | None = None,
        paths: list[str] | None = None,
        include_command_effects: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        """Rollback recorded file changes for an Open Subagent MCP run or segment.

        By default this also reverts command side effects that were detected by
        filesystem scans. The rollback refuses conflicts unless force is true.
        """
        return await runtime.rollback_agent(
            {
                "agent_id": agent_id,
                "segment_id": segment_id,
                "paths": paths or [],
                "include_command_effects": include_command_effects,
                "force": force,
            }
        )

    return server


def run_stdio() -> None:
    create_server().run(transport="stdio")
