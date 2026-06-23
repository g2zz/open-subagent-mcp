# Migrating From The Gemini Agent Local Version

Open Subagent MCP is the generalized public version of an earlier local
Gemini-specific project. The public version intentionally uses new names and does
not expose the old tool aliases.

## Name Mapping

| Old | New |
| --- | --- |
| `gemini-agent` | `open-subagent-mcp` |
| `gemini_agent` | `open_subagent_mcp` |
| `Gemini Agent` | `Open Subagent MCP` |
| `mcp_servers.gemini_agent` | `mcp_servers.open_subagent_mcp` |
| `gemini_agent_spawn_agent` | `subagent_spawn` |
| `gemini_agent_wait_agent` | `subagent_wait` |
| `gemini_agent_send_input` | `subagent_send_message` |
| `gemini_agent_close_agent` | `subagent_close` |
| `gemini_agent_rollback_agent` | `subagent_rollback` |
| `GEMINI_AGENT_*` | `SUBAGENT_MCP_*` |

## Configuration

Provider configuration still uses:

- `OPENAI_BASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_MODEL_NAME`

Runtime-specific variables now use the `SUBAGENT_MCP_` prefix.

## Breaking Change

The public version does not keep `gemini_agent_*` tool aliases. Update MCP host
configuration and any skills, prompts, or scripts that reference the old names.

## Recommended Migration

1. Install Open Subagent MCP in a new directory.
2. Configure it under a new MCP server key: `open_subagent_mcp`.
3. Restart your MCP host.
4. Verify `subagent_*` tools appear.
5. Remove the old Gemini-specific MCP server only after the new server passes
   smoke tests.
