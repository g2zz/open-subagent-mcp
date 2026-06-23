# Agent Setup Guide

This guide is written for a coding agent or assistant that is installing Open
Subagent MCP on a user's machine.

## Goal

Install the local stdio MCP server, configure the user's MCP host, and verify the
server exposes the `subagent_*` tools.

## Preconditions

- Python 3.11 or newer is available.
- The user has chosen an OpenAI-compatible Chat Completions endpoint.
- The user has approved sending ordinary workspace code and documentation to
  that endpoint for subagent tasks.
- The target MCP host supports local stdio MCP servers.

## Install

```bash
git clone https://github.com/g2zz/open-subagent-mcp.git ~/open-subagent-mcp
cd ~/open-subagent-mcp
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Verify the CLI:

```bash
~/open-subagent-mcp/.venv/bin/open-subagent-mcp --help
python -m open_subagent_mcp --help
```

## Configure Codex

Add this block to `~/.codex/config.toml`, replacing endpoint values:

```toml
[mcp_servers.open_subagent_mcp]
command = "/Users/<user>/open-subagent-mcp/.venv/bin/open-subagent-mcp"
args = []
startup_timeout_sec = 20
tool_timeout_sec = 180

[mcp_servers.open_subagent_mcp.env]
OPENAI_BASE_URL = "http://localhost:8000/v1"
OPENAI_API_KEY = "your-api-key"
OPENAI_MODEL_NAME = "your-model-name"
```

Restart Codex after editing MCP configuration.

## Configure Claude Code

Use Claude Code's stdio MCP command form:

```bash
claude mcp add --transport stdio open-subagent-mcp \
  --env OPENAI_BASE_URL=http://localhost:8000/v1 \
  --env OPENAI_API_KEY=your-api-key \
  --env OPENAI_MODEL_NAME=your-model-name \
  -- /Users/<user>/open-subagent-mcp/.venv/bin/open-subagent-mcp
```

Then run `/mcp` inside Claude Code to inspect server status.

## Verify

```bash
cd ~/open-subagent-mcp
. .venv/bin/activate
pytest -q
python scripts/smoke_mcp_stdio.py
```

If a real provider is available:

```bash
OPENAI_BASE_URL=http://localhost:8000/v1 \
OPENAI_API_KEY=your-api-key \
OPENAI_MODEL_NAME=your-model-name \
RUN_REAL_LLM_SMOKE=1 \
python scripts/smoke_openai_compatible.py
```

## Do Not Copy

Do not copy another user's:

- `.venv/`
- `.runs/`
- local MCP host config
- provider API keys
- private workspace paths
- private logs

