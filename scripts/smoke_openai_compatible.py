from __future__ import annotations

import asyncio
import json
import os

from open_subagent_mcp.actions import ActionParseError, parse_action
from open_subagent_mcp.config import load_settings
from open_subagent_mcp.llm_client import OpenAICompatibleClient
from open_subagent_mcp.prompts import SYSTEM_PROMPT, build_repair_prompt


async def main() -> None:
    if os.getenv("RUN_REAL_LLM_SMOKE") != "1":
        print(json.dumps({"ok": True, "skipped": True, "reason": "RUN_REAL_LLM_SMOKE is not 1"}))
        return
    settings = load_settings()
    client = OpenAICompatibleClient(settings, timeout_seconds=60)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "只输出下面这个 JSON 对象，不要输出 Markdown，不要省略 args，不要把 args 内字段放到顶层：\n"
                '{"action":"finish","args":{"status":"completed","summary":"real smoke ok",'
                '"self_check_commands":["real smoke"],"tests":["chat completions HTTP 200"],'
                '"risk_notes":["smoke only"],"open_issues":[]}}'
            ),
        },
    ]
    last_result = None
    parsed = None
    repairs = 0
    for attempt in range(3):
        last_result = await client.chat(
            model=settings.openai_model_name,
            messages=messages,
            temperature=0.1,
        )
        try:
            parsed = parse_action(last_result.content)
            break
        except ActionParseError as exc:
            repairs += 1
            messages.append({"role": "assistant", "content": last_result.content})
            messages.append({"role": "user", "content": build_repair_prompt(last_result.content, str(exc))})
    if parsed is None or last_result is None:
        raise RuntimeError("real LLM smoke did not produce a valid JSON action after repairs")
    print(
        json.dumps(
            {
                "ok": True,
                "finish_reason": last_result.finish_reason,
                "action": parsed.action,
                "model": settings.openai_model_name,
                "repairs": repairs,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
