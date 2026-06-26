from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .config import Settings
from .models import ErrorCode

TRANSIENT_HTTP_STATUS_CODES = {429, 500, 502, 503, 504, 529}
MAX_HTTP_ATTEMPTS = 6
RETRY_BASE_DELAY_SECONDS = 0.5
RETRY_MAX_DELAY_SECONDS = 5.0
COMPACT_RETRY_AFTER_ATTEMPTS = 3
COMPACT_RETRY_KEEP_RECENT_MESSAGES = 8
COMPACT_RETRY_MAX_MESSAGE_CHARS = 4000


class LLMError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass
class ChatResult:
    content: str
    finish_reason: str | None
    raw: dict[str, Any]


class ChatClient(Protocol):
    async def chat(self, *, model: str, messages: list[dict[str, str]], temperature: float = 0.1) -> ChatResult:
        ...


class OpenAICompatibleClient:
    def __init__(
        self,
        settings: Settings,
        *,
        timeout_seconds: int = 60,
        transport: httpx.AsyncBaseTransport | None = None,
        max_attempts: int = MAX_HTTP_ATTEMPTS,
        retry_base_delay_seconds: float = RETRY_BASE_DELAY_SECONDS,
        retry_max_delay_seconds: float = RETRY_MAX_DELAY_SECONDS,
    ) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.max_attempts = max_attempts
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds

    async def chat(self, *, model: str, messages: list[dict[str, str]], temperature: float = 0.1) -> ChatResult:
        url = self.settings.openai_base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        active_messages = messages
        payload = {"model": model, "messages": active_messages, "temperature": temperature}
        last_exc: Exception | None = None
        length_retry_used = False
        compact_retry_used = False
        attempts: list[dict[str, Any]] = []
        for attempt in range(self.max_attempts):
            request_diagnostics = _message_diagnostics(model, active_messages)
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                    response = await client.post(url, headers=headers, json=payload)
                response_record = {
                    "attempt": attempt + 1,
                    "status_code": response.status_code,
                    "transient": response.status_code in TRANSIENT_HTTP_STATUS_CODES,
                    "body": response.text[:2000],
                    "request": request_diagnostics,
                    "compact_retry_used": compact_retry_used,
                }
                attempts.append(response_record)
                if response.status_code in TRANSIENT_HTTP_STATUS_CODES and attempt < self.max_attempts - 1:
                    if not compact_retry_used and attempt + 1 >= COMPACT_RETRY_AFTER_ATTEMPTS:
                        active_messages = _compact_messages_for_retry(messages)
                        payload = {"model": model, "messages": active_messages, "temperature": temperature}
                        compact_retry_used = True
                        attempts.append(
                            {
                                "attempt": attempt + 1,
                                "event": "compact_retry_enabled",
                                "request": _message_diagnostics(model, active_messages),
                            }
                        )
                    await asyncio.sleep(self._retry_delay(attempt, response))
                    continue
                if response.status_code >= 400:
                    raise LLMError(
                        ErrorCode.llm_http_error,
                        f"LLM HTTP {response.status_code} after {attempt + 1} attempt(s)",
                        {
                            "status_code": response.status_code,
                            "body": response.text[:2000],
                            "attempts": attempts,
                            "max_attempts": self.max_attempts,
                            "retryable_status": response.status_code in TRANSIENT_HTTP_STATUS_CODES,
                            "request": request_diagnostics,
                            "compact_retry_used": compact_retry_used,
                        },
                    )
                raw = response.json()
                try:
                    return _parse_chat_response(raw)
                except LLMError as exc:
                    if exc.code == ErrorCode.llm_truncated and not length_retry_used:
                        length_retry_used = True
                        continue
                    raise
            except LLMError:
                raise
            except Exception as exc:
                last_exc = exc
                attempts.append(
                    {
                        "attempt": attempt + 1,
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                        "request": request_diagnostics,
                        "compact_retry_used": compact_retry_used,
                    }
                )
                if not compact_retry_used and attempt + 1 >= COMPACT_RETRY_AFTER_ATTEMPTS:
                    active_messages = _compact_messages_for_retry(messages)
                    payload = {"model": model, "messages": active_messages, "temperature": temperature}
                    compact_retry_used = True
                    attempts.append(
                        {
                            "attempt": attempt + 1,
                            "event": "compact_retry_enabled",
                            "request": _message_diagnostics(model, active_messages),
                        }
                    )
                if attempt < self.max_attempts - 1:
                    await asyncio.sleep(self._retry_delay(attempt, None))
                    continue
        raise LLMError(
            ErrorCode.llm_http_error,
            str(last_exc or "unknown LLM error"),
            {
                "attempts": attempts,
                "max_attempts": self.max_attempts,
                "last_exception_type": type(last_exc).__name__ if last_exc else None,
                "request": _message_diagnostics(model, active_messages),
                "compact_retry_used": compact_retry_used,
            },
        )

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    return min(float(retry_after), self.retry_max_delay_seconds)
                except ValueError:
                    pass
        return min(self.retry_base_delay_seconds * (2**attempt), self.retry_max_delay_seconds)


def _parse_chat_response(raw: dict[str, Any]) -> ChatResult:
    try:
        choice = raw["choices"][0]
        content = choice["message"]["content"]
    except Exception as exc:
        raise LLMError(ErrorCode.llm_http_error, "missing choices[0].message.content", {"raw": raw}) from exc
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise LLMError(ErrorCode.llm_truncated, "LLM response was truncated")
    return ChatResult(content=content, finish_reason=finish_reason, raw=raw)


def _message_diagnostics(model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    lengths = [len(message.get("content", "")) for message in messages]
    roles = [message.get("role", "") for message in messages]
    return {
        "model": model,
        "message_count": len(messages),
        "total_chars": sum(lengths),
        "max_message_chars": max(lengths, default=0),
        "last_message_chars": lengths[-1] if lengths else 0,
        "roles": roles[-8:],
    }


def _compact_messages_for_retry(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    if len(messages) <= COMPACT_RETRY_KEEP_RECENT_MESSAGES + 2:
        return [_truncate_message(message) for message in messages]

    compacted: list[dict[str, str]] = []
    if messages and messages[0].get("role") == "system":
        compacted.append(_truncate_message(messages[0]))
        remaining = messages[1:]
    else:
        remaining = messages

    if remaining:
        compacted.append(_truncate_message(remaining[0]))

    omitted = max(0, len(remaining) - 1 - COMPACT_RETRY_KEEP_RECENT_MESSAGES)
    if omitted:
        compacted.append(
            {
                "role": "user",
                "content": (
                    "[context compacted after repeated transient LLM HTTP errors]\n"
                    f"Omitted {omitted} older tool-loop message(s). Continue from the recent evidence below. "
                    "If key evidence is missing, request a narrower read/search instead of guessing."
                ),
            }
        )

    compacted.extend(_truncate_message(message) for message in remaining[-COMPACT_RETRY_KEEP_RECENT_MESSAGES:])
    return compacted


def _truncate_message(message: dict[str, str]) -> dict[str, str]:
    content = message.get("content", "")
    if len(content) <= COMPACT_RETRY_MAX_MESSAGE_CHARS:
        return dict(message)
    return {
        **message,
        "content": content[:COMPACT_RETRY_MAX_MESSAGE_CHARS]
        + f"\n[message truncated for compact retry; original_chars={len(content)}]",
    }


class FakeLLMClient:
    def __init__(self, outputs: list[str | Exception | dict[str, Any]]) -> None:
        self.outputs = list(outputs)
        self.requests: list[dict[str, Any]] = []

    async def chat(self, *, model: str, messages: list[dict[str, str]], temperature: float = 0.1) -> ChatResult:
        self.requests.append({"model": model, "messages": messages, "temperature": temperature})
        if not self.outputs:
            raise LLMError(ErrorCode.llm_http_error, "fake LLM has no more outputs")
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return _parse_chat_response(item)
        return ChatResult(
            content=item,
            finish_reason="stop",
            raw={"choices": [{"message": {"content": item}, "finish_reason": "stop"}]},
        )
