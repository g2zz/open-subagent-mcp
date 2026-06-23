from __future__ import annotations

import json

from open_subagent_mcp.prompts import SYSTEM_PROMPT, build_repair_prompt, build_task_package


def test_system_prompt_contains_required_constraints() -> None:
    assert "JSON action" in SYSTEM_PROMPT
    assert "MCP host/orchestrator" in SYSTEM_PROMPT
    assert "权限边界" in SYSTEM_PROMPT
    assert "自检" in SYSTEM_PROMPT
    assert "repo_map" in SYSTEM_PROMPT
    assert "read_many_files" in SYSTEM_PROMPT
    assert "use_skill_context" in SYSTEM_PROMPT
    assert "request_main_tool" in SYSTEM_PROMPT
    assert "不允许空结果" in SYSTEM_PROMPT


def test_repair_prompt_mentions_schema_error_and_json_only() -> None:
    prompt = build_repair_prompt("bad", "missing args")
    assert "schema" in prompt
    assert "合法 JSON action" in prompt
    assert "missing args" in prompt


def test_task_package_includes_action_arg_schema() -> None:
    prompt = build_task_package(
        agent_id="run_test",
        agent_type="explorer",
        cwd="/tmp/project",
        message="read README",
        current_segment_id="seg_0001",
        authorizations=[],
        dry_run=False,
    )
    payload = json.loads(prompt.removeprefix("任务包：\n"))
    finish_schema = payload["json_action_contract"]["args_schema_by_action"]["finish"]
    assert finish_schema["additionalProperties"] is False
    assert finish_schema["required"] == [
        "status",
        "summary",
        "self_check_commands",
        "tests",
        "risk_notes",
        "open_issues",
    ]
    assert "valid_finish_completed_example" in payload["json_action_contract"]
    contract = payload["json_action_contract"]
    assert "read_many_files" in contract["args_schema_by_action"]
    assert "repo_map" in contract["args_schema_by_action"]
    assert "run_tests" in contract["args_schema_by_action"]
    assert "request_main_tool" in contract["args_schema_by_action"]
    assert "use_skill_context" in contract["args_schema_by_action"]
    assert contract["tool_guidance_by_action"]["request_main_tool"]["after_call_behavior"].startswith("Stop.")


def test_task_package_exposes_item_catalog_without_full_text() -> None:
    prompt = build_task_package(
        agent_id="run_test",
        agent_type="worker",
        cwd="/tmp/project",
        message="use skill",
        current_segment_id="seg_0001",
        authorizations=[],
        dry_run=False,
        item_catalog=[
            {
                "item_id": "item_0001",
                "segment_id": "seg_0001",
                "type": "text",
                "name": "skill:demo",
                "size": 1000,
                "preview": "short preview",
                "preview_truncated": True,
            }
        ],
    )
    payload = json.loads(prompt.removeprefix("任务包：\n"))
    assert payload["item_catalog"][0]["preview"] == "short preview"
    assert "text" not in payload["item_catalog"][0]
    assert payload["required_context_items"][0]["name"] == "skill:demo"


def test_repair_prompt_includes_valid_finish_example() -> None:
    prompt = build_repair_prompt('{"action":"finish","args":{"message":"done"}}', "finish fields missing")
    assert '"action": "finish"' in prompt or '"action":"finish"' in prompt
    assert '"status": "completed"' in prompt
    assert '"risk_notes"' in prompt
    assert "不要重复执行已经成功的 action" in prompt
