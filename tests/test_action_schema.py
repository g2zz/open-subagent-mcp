from __future__ import annotations

import json

import pytest

from open_subagent_mcp.actions import ActionParseError, observation_ok, parse_action
from open_subagent_mcp.models import ErrorCode


def test_parse_valid_action() -> None:
    parsed = parse_action(json.dumps({"action": "read_file", "args": {"path": "README.md"}}))
    assert parsed.action == "read_file"
    assert parsed.args["max_lines"] == 400


def test_rejects_invalid_json() -> None:
    with pytest.raises(ActionParseError) as exc:
        parse_action("{not json")
    assert exc.value.code == ErrorCode.model_output_parse_error


def test_parse_markdown_fenced_json_action() -> None:
    parsed = parse_action(
        """```json
{"action": "read_file", "args": {"path": "README.md"}}
```"""
    )
    assert parsed.action == "read_file"


def test_parse_embedded_json_action() -> None:
    parsed = parse_action(
        'Here is the action:\n{"action": "read_file", "args": {"path": "README.md"}}\nDone.'
    )
    assert parsed.action == "read_file"


def test_parse_json_action_with_trailing_comma() -> None:
    parsed = parse_action(
        """```json
{
  "action": "read_file",
  "args": {
    "path": "README.md",
  },
}
```"""
    )
    assert parsed.action == "read_file"


def test_rejects_extra_fields() -> None:
    with pytest.raises(ActionParseError) as exc:
        parse_action(json.dumps({"action": "read_file", "args": {"path": "x"}, "extra": True}))
    assert exc.value.code == ErrorCode.action_schema_error


def test_finish_requires_verification_for_completed() -> None:
    with pytest.raises(ActionParseError):
        parse_action(
            json.dumps(
                {
                    "action": "finish",
                    "args": {
                        "status": "completed",
                        "summary": "done",
                        "self_check_commands": [],
                        "tests": [],
                        "risk_notes": [],
                        "open_issues": [],
                    },
                }
            )
        )


def test_finish_requires_spec_fields() -> None:
    with pytest.raises(ActionParseError) as exc:
        parse_action(json.dumps({"action": "finish", "args": {"status": "failed", "summary": "bad"}}))
    assert exc.value.code == ErrorCode.action_schema_error


def test_new_actions_require_reason_where_expected() -> None:
    for action, args in [
        ("read_many_files", {"files": [{"path": "README.md"}]}),
        ("repo_map", {}),
        ("run_tests", {"cmd": "pytest -q"}),
        ("request_main_tool", {"tool": "web_search", "input": {}, "expected_output": "links"}),
        ("use_skill_context", {"name": "skill"}),
    ]:
        with pytest.raises(ActionParseError) as exc:
            parse_action(json.dumps({"action": action, "args": args}))
        assert exc.value.code == ErrorCode.action_schema_error


def test_parse_request_main_tool_action() -> None:
    parsed = parse_action(
        json.dumps(
            {
                "action": "request_main_tool",
                "args": {
                    "reason": "Need current docs.",
                    "tool": "web_search",
                    "input": {"query": "Open Subagent MCP"},
                    "expected_output": "official links",
                    "sensitivity": "public",
                },
            }
        )
    )
    assert parsed.action == "request_main_tool"
    assert parsed.args["reason"] == "Need current docs."


def test_observation_redacts_secret_like_values() -> None:
    obs = observation_ok("act_1", {"text": "api_key=sk-abcdefghi token=abc123456789"})
    dumped = obs.model_dump_json()
    assert "abcdefghi" not in dumped
    assert "abc123456789" not in dumped


def test_observation_redaction_preserves_json_with_escaped_quotes() -> None:
    obs = observation_ok(
        "act_1",
        {
            "content": (
                'env = { OPENAI_BASE_URL = "http://localhost:8000/v1", '
                'OPENAI_API_KEY = "YOUR_API_KEY", OPENAI_MODEL_NAME = "openai-compatible-model" }'
            )
        },
    )
    dumped = obs.model_dump_json()
    assert "OPENAI_API_KEY" in dumped
    assert "YOUR_API_KEY" not in dumped
