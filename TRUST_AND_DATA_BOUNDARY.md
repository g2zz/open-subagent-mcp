# Trust And Data Boundary

This document is for users, MCP host administrators, and security reviewers. It
describes what Open Subagent MCP may receive, where data flows, and what
protections are implemented.

Open Subagent MCP is evidence for a trust decision. It does not make any model
endpoint trustworthy by itself.

## Intended Boundary

Open Subagent MCP is a local `stdio` MCP server started by an MCP host on the
user's workstation. It is intended for user-authorized coding tasks that may
require reading source code, tests, documentation, and build metadata from a
local workspace.

The configured `OPENAI_BASE_URL` receives selected subagent conversation
messages and file observations. Users and organizations must decide whether that
endpoint is allowed to receive the target workspace content.

## Data Flow

1. The MCP host calls one of the `subagent_*` tools.
2. Open Subagent MCP starts or resumes a local run under `SUBAGENT_MCP_RUNS_DIR`
   or `.runs/`.
3. The subagent model receives the task package and later receives selected
   observations from local actions.
4. File access is bounded by `cwd` and optional `allowed_external_roots`.
5. Raw model outputs, validated actions, observations, command logs, state, and
   rollback metadata are stored locally for audit.

## Allowed Data

Open Subagent MCP may receive ordinary source code, tests, documentation, and
build metadata from user-authorized workspaces when the task requires it.

External paths are blocked unless the request explicitly declares
`allowed_external_roots` and those paths pass realpath, symlink, and sensitive
path checks.

## Disallowed Data

Do not send these data types to Open Subagent MCP:

- Credentials, tokens, API keys, private keys, certificates, or signing secrets.
- `.env`, `.ssh`, `.aws`, `.gcloud`, keychain exports, or credential stores.
- Production customer data, operational data, incident data, or regulated data
  unless your organization explicitly authorizes the exact workflow.
- Paths outside `cwd` that were not declared in `allowed_external_roots`.
- Content requested only to route around host approval, review, or policy.

## Implemented Protections

- `realpath` path normalization before file access.
- Workspace boundary checks for `cwd` and `allowed_external_roots`.
- Symlink escape checks.
- Sensitive path and filename blocking.
- Separate `explorer` and `worker` modes, where `explorer` is read-only.
- JSON schema validation for every subagent action.
- Raw model output persistence for audit and replay.
- File write snapshots and rollback metadata.
- Command before/after filesystem scans for local file changes.
- Segment-level rollback for each `subagent_spawn` and `subagent_send_message`.
- Local stdout/stderr command capture with truncated MCP tool responses.

## Rollback Boundary

Rollback is best-effort local file rollback. It is based on recorded file
snapshots, write logs, command side-effect scans, and rollback segments.

Rollback does not undo:

- Network requests.
- Database writes.
- Cloud or production operations.
- External API side effects.
- Long-lived processes.
- File changes outside recorded paths.
- Changes blocked by conflicts unless force is explicitly used.

## Host Policy

Hosts should keep a human in the loop for risky tool calls, especially
`subagent_spawn` with `agent_type="worker"`, long-running commands, external
roots, dependency installs, destructive actions, and production operations.

Open Subagent MCP should be connected only to endpoints that the user or
organization trusts to receive the requested workspace content.
