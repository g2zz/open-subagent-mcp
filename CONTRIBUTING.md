# Contributing

Thanks for improving Open Subagent MCP.

## Development Setup

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Checks

Run deterministic checks before opening a PR:

```bash
ruff check .
pytest -q
python scripts/smoke_mcp_stdio.py
python scripts/eval_runtime_fake.py
python scripts/eval_mcp_blackbox.py
python scripts/eval_security_adversarial.py
```

Real provider checks are optional and require your own OpenAI-compatible
endpoint:

```bash
RUN_REAL_LLM_SMOKE=1 python scripts/smoke_openai_compatible.py
RUN_REAL_LLM_EVAL=1 python scripts/eval_real_subagent_canary.py
```

## PR Guidelines

- Keep public MCP tool names stable unless the change is explicitly breaking.
- Do not add stdout logging to MCP server startup or tool code. Stdio stdout must
  contain only valid MCP messages.
- Add tests for new actions, state transitions, and rollback behavior.
- Update README and SECURITY.md when changing data flow, permissions, command
  execution, or rollback behavior.
- Do not commit `.venv/`, `.runs/`, private logs, local MCP host configs, or
  provider credentials.

