from __future__ import annotations

import json

import httpx
import pytest

from open_subagent_mcp.config import Settings
from open_subagent_mcp.llm_client import LLMError, OpenAICompatibleClient
from open_subagent_mcp.models import ErrorCode


@pytest.mark.asyncio
async def test_http_500_retries_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, json={"error": "busy"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{\"action\":\"finish\",\"args\":{}}"}, "finish_reason": "stop"}]},
        )

    client = OpenAICompatibleClient(Settings(), transport=httpx.MockTransport(handler), retry_base_delay_seconds=0)
    result = await client.chat(model="m", messages=[])
    assert result.finish_reason == "stop"
    assert calls == 2


@pytest.mark.asyncio
async def test_http_529_retries_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(529, json={"error": "provider resource exhausted"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{\"action\":\"finish\",\"args\":{}}"}, "finish_reason": "stop"}]},
        )

    client = OpenAICompatibleClient(Settings(), transport=httpx.MockTransport(handler), retry_base_delay_seconds=0)
    result = await client.chat(model="m", messages=[])
    assert result.finish_reason == "stop"
    assert calls == 3


@pytest.mark.asyncio
async def test_http_502_exhaustion_reports_attempt_history() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, text="bad gateway")

    client = OpenAICompatibleClient(
        Settings(),
        transport=httpx.MockTransport(handler),
        max_attempts=3,
        retry_base_delay_seconds=0,
    )
    with pytest.raises(LLMError) as exc:
        await client.chat(model="m", messages=[])
    assert exc.value.code == ErrorCode.llm_http_error
    assert "LLM HTTP 502 after 3 attempt(s)" in str(exc.value)
    assert exc.value.details["status_code"] == 502
    assert exc.value.details["max_attempts"] == 3
    assert exc.value.details["retryable_status"] is True
    assert [item["status_code"] for item in exc.value.details["attempts"]] == [502, 502, 502]
    assert exc.value.details["request"]["model"] == "m"
    assert exc.value.details["request"]["message_count"] == 0
    assert calls == 3


@pytest.mark.asyncio
async def test_transient_errors_retry_with_compacted_messages() -> None:
    calls = 0
    message_counts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        message_counts.append(len(body["messages"]))
        if calls < 4:
            return httpx.Response(500, text="")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{\"action\":\"finish\",\"args\":{}}"}, "finish_reason": "stop"}]},
        )

    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
    messages.extend({"role": "user", "content": f"observation {index} " + ("x" * 5000)} for index in range(14))
    client = OpenAICompatibleClient(
        Settings(),
        transport=httpx.MockTransport(handler),
        max_attempts=5,
        retry_base_delay_seconds=0,
    )
    result = await client.chat(model="m", messages=messages)
    assert result.finish_reason == "stop"
    assert calls == 4
    assert message_counts[:3] == [16, 16, 16]
    assert message_counts[3] < 16


@pytest.mark.asyncio
async def test_missing_choices_is_llm_http_error() -> None:
    client = OpenAICompatibleClient(Settings(), transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    with pytest.raises(LLMError) as exc:
        await client.chat(model="m", messages=[])
    assert exc.value.code == ErrorCode.llm_http_error


@pytest.mark.asyncio
async def test_length_finish_reason_retries_once_then_fails() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "cut"}, "finish_reason": "length"}]},
        )

    client = OpenAICompatibleClient(Settings(), transport=httpx.MockTransport(handler), retry_base_delay_seconds=0)
    with pytest.raises(LLMError) as exc:
        await client.chat(model="m", messages=[])
    assert exc.value.code == ErrorCode.llm_truncated
    assert calls == 2
