# Security Policy

Open Subagent MCP is a local developer-workstation tool. It can read files, write
files, run commands, send selected workspace observations to a configured model
endpoint, and attempt best-effort rollback of recorded local file changes.

It is not a sandbox, container, policy engine, or production isolation boundary.

## Supported Versions

Security fixes target the latest released version. Pre-1.0 releases may include
breaking changes when required to fix security or data-boundary issues.

## Reporting A Vulnerability

If GitHub private vulnerability reporting is enabled for this repository, use it.
Otherwise, open a GitHub issue with a minimal description and avoid posting
secrets, private code, exploit payloads, or sensitive logs.

Please include:

- Affected version or commit.
- Host application and operating system.
- Minimal reproduction steps.
- Whether the issue requires a malicious model, malicious repository, or
  malicious MCP host.
- Expected and actual behavior.

## Security Model

Open Subagent MCP assumes:

- The user intentionally installed and configured the MCP server.
- The MCP host decides which tools are exposed and when the user must approve
  tool calls.
- The configured `OPENAI_BASE_URL` is trusted by the user or organization to
  receive the workspace content used in a task.
- The local workstation account has normal user permissions.

Open Subagent MCP does not assume that repositories, model outputs, or injected
context are trustworthy.

## High-Risk Capabilities

Treat these as high risk:

- `agent_type="worker"`
- `run_command`
- `run_tests`
- `apply_patch`
- `write_file`
- external roots
- long-running commands
- dependency installation
- destructive actions
- production operations

## Rollback Limits

Rollback is best-effort local file rollback. It does not reverse network calls,
database writes, cloud actions, production operations, long-lived processes, or
untracked effects outside recorded file changes.

Do not connect this tool directly to production systems.

