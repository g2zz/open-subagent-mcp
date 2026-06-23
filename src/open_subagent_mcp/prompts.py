from __future__ import annotations

import json
from typing import Any

from .actions import ACTION_ARG_MODELS, ACTION_TOOL_GUIDANCE

SYSTEM_PROMPT = """你是 Open Subagent，由 MCP host/orchestrator 调度。
你是执行型 subagent，不是最终答复者；最终结果由 MCP host/orchestrator 负责，你不能直接对用户承诺最终结果。
每轮只能输出一个 JSON action，不要输出 Markdown、解释文字或代码围栏。
不确定时使用只读 action 获取信息，不要猜测。
写入、patch、测试前必须先读取相关文件或注入上下文；写入前优先使用 apply_patch。
如果任务包包含 required_context_items，必须先用 use_skill_context 读取相关说明，不能凭通用记忆硬干。
优先使用 repo_map、read_many_files、search 这类结构化只读 action；不要一上来用宽泛 run_command。
写入、patch、测试、请求主工具时必须 reason-first：先说明为什么做，再给路径、命令或内容。
需要 web、browser、image、node、skill 解释或其他 host/orchestrator 专属能力时，必须用 request_main_tool，然后停止等待 host/orchestrator 回填结果；不要伪造外部工具结果。
完成前必须自检；无法自检时在 finish 中说明原因、风险和剩余问题。
失败时必须 finish(status="failed") 或 waiting_input，带已有证据、阻塞点、下一步需要什么；不允许空结果。
不要读取凭证、密钥或生产信息，除非任务明确授权。
安全拒绝只讲原则和必要信息，不暴露可绕过的检测机制。
遵守 runtime 给出的权限边界、目标 cwd、allowed_external_roots 和当前 rollback segment。
搜索和命令必须先收窄路径、文件名或 glob；不要一上来扫描整个仓库、.conda、.venv、node_modules、缓存目录或大数据文件。
复杂探索优先增加 max_steps 或拆分任务，不要用宽泛命令制造大量无关输出。
"""


FINISH_COMPLETED_EXAMPLE = {
    "action": "finish",
    "args": {
        "status": "completed",
        "summary": "已完成任务并记录关键结果。",
        "self_check_commands": [],
        "tests": ["已检查相关 observation"],
        "risk_notes": ["未运行命令；结果基于已读取文件和 observation"],
        "open_issues": [],
    },
}


def build_json_action_contract() -> dict[str, Any]:
    return {
        "shape": {"action": "one of the supported action names", "args": "object matching that action schema"},
        "strict_rules": [
            "Only output one JSON object per assistant turn.",
            "Do not output Markdown, code fences, comments, or extra top-level keys.",
            "Every args object has additionalProperties=false; unknown fields are rejected.",
            "For reason-first actions, put reason first in args before paths, commands, or content.",
            "If request_main_tool is used, stop and wait for the MCP host/orchestrator to send input back.",
            "If required_context_items is non-empty, use use_skill_context before write/test/patch actions.",
            "For finish.status=completed, open_issues must be empty.",
            "For finish.status=completed, at least one of self_check_commands/tests/risk_notes must be non-empty.",
        ],
        "args_schema_by_action": {name: model.model_json_schema() for name, model in ACTION_ARG_MODELS.items()},
        "tool_guidance_by_action": ACTION_TOOL_GUIDANCE,
        "valid_finish_completed_example": FINISH_COMPLETED_EXAMPLE,
    }


def build_task_package(
    *,
    agent_id: str,
    agent_type: str,
    cwd: str,
    message: str,
    current_segment_id: str,
    authorizations: list[str],
    dry_run: bool,
    item_catalog: list[dict[str, Any]] | None = None,
) -> str:
    items = item_catalog or []
    required_context_items = [
        item
        for item in items
        if item.get("type") == "text" and ("skill" in str(item.get("name", "")).lower() or "context" in str(item.get("name", "")).lower())
    ]
    payload = {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "cwd": cwd,
        "current_segment_id": current_segment_id,
        "task": message,
        "explicit_authorizations": authorizations,
        "dry_run": dry_run,
        "item_catalog": items,
        "required_context_items": required_context_items,
        "execution_guidance": [
            "Use agent_type=explorer for read-only exploration and agent_type=worker for writable tasks.",
            "Keep timeout_seconds <= 120 unless explicit_authorizations includes long_running_commands.",
            "For complex exploration, prefer higher max_steps and narrow searches over longer timeouts.",
            "If required_context_items is not empty, call use_skill_context before write/test/patch actions.",
            "Use request_main_tool for web/browser/node/image/skill requests, then stop and wait for the MCP host/orchestrator.",
            "Narrow search/list paths before using run_command; avoid .conda, .venv, node_modules, caches, and large data files.",
        ],
        "json_action_contract": build_json_action_contract(),
    }
    return "任务包：\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def build_repair_prompt(raw_output: str, error: str) -> str:
    contract = json.dumps(build_json_action_contract(), ensure_ascii=False, indent=2)
    finish_example = json.dumps(FINISH_COMPLETED_EXAMPLE, ensure_ascii=False)
    return (
        "上一轮输出无法解析或不符合 schema。\n"
        f"具体错误字段或原因：{error}\n"
        "请只重发一个合法 JSON action，不要输出 Markdown 或解释。\n"
        "如果上一轮已经完成任务但 finish 不合法，请优先修正为合法 finish，不要重复执行已经成功的 action。\n"
        "合法 JSON action contract：\n"
        f"{contract}\n"
        "最小合法 completed finish 示例：\n"
        f"{finish_example}\n"
        f"上一轮原始输出：{raw_output[:4000]}"
    )
