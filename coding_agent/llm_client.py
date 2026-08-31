"""Small OpenAI-compatible Chat Completions client using plain HTTP.

This module intentionally contains no Agent framework or provider SDK.  It
only serializes a Chat Completions request and normalizes the JSON response;
the Agent loop and local tool execution live elsewhere in the project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http.client import IncompleteRead, RemoteDisconnected
import json
import socket
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import AppConfig

Message = Mapping[str, Any]
ToolDefinition = Mapping[str, Any]
HttpPost = Callable[[str, Mapping[str, str], bytes, float], tuple[int, bytes]]
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class LLMClientError(RuntimeError):
    """Raised when an LLM response cannot be used by the local runtime."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments_json: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    model: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)

    @property
    def requests_tools(self) -> bool:
        return bool(self.tool_calls)


def _http_post(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    request_headers = dict(headers)
    # DeepSeek/OpenAI-compatible gateways occasionally close an idle reused
    # connection before sending the response. Force a fresh HTTP/1.1 request
    # and avoid content compression so partial-body failures are retryable.
    request_headers.setdefault("Connection", "close")
    request_headers.setdefault("Accept", "application/json")
    request_headers.setdefault("Accept-Encoding", "identity")
    request = Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except HTTPError as exc:
        return int(exc.code), exc.read()
    except (IncompleteRead, RemoteDisconnected, ConnectionResetError, socket.timeout) as exc:
        raise LLMClientError(f"模型连接中断：{type(exc).__name__}") from exc
    except URLError as exc:
        raise LLMClientError(f"模型网络请求失败：{exc.reason}") from exc


class OpenAICompatibleClient:
    """Call an OpenAI-compatible endpoint via plain HTTP only."""

    def __init__(self, config: AppConfig, *, http_post: HttpPost | None = None) -> None:
        self._config = config
        self._http_post = http_post or _http_post

    def complete(self, messages: Sequence[Message], *, tools: Sequence[ToolDefinition] | None = None) -> ModelResponse:
        if not messages:
            raise ValueError("messages 不能为空。")
        base_url = (self._config.base_url or "https://api.openai.com/v1").rstrip("/")
        request: dict[str, Any] = {"model": self._config.model, "messages": list(messages)}
        if tools:
            request["tools"] = list(tools)
            request["tool_choice"] = "auto"
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(max(1, getattr(self._config, "max_retries", 3))):
            try:
                status, raw = self._http_post(f"{base_url}/chat/completions", headers, body, self._config.request_timeout)
                if status == 200:
                    return self._parse_response(raw)
                detail = raw.decode("utf-8", errors="replace")[:500]
                if status not in _RETRYABLE_STATUS:
                    raise LLMClientError(f"模型请求失败：HTTP {status}: {detail}")
                last_error = LLMClientError(f"HTTP {status}: {detail}")
            except LLMClientError as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc
            if attempt < max(1, getattr(self._config, "max_retries", 3)) - 1:
                time.sleep(2 ** attempt)
        detail = str(last_error).replace(self._config.api_key, "[REDACTED]")
        raise LLMClientError(f"模型请求在重试后仍失败：{detail}") from last_error

    @staticmethod
    def _parse_response(raw: bytes) -> ModelResponse:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMClientError("模型响应格式无效。") from exc
        choices = payload.get("choices") if isinstance(payload, Mapping) else None
        if not choices:
            raise LLMClientError("模型响应中没有 choices。")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, Mapping) else None
        if not isinstance(message, Mapping):
            raise LLMClientError("模型响应中没有 message。")
        calls = tuple(
            ToolCall(str(call.get("id", "")), str((call.get("function") or {}).get("name", "")), str((call.get("function") or {}).get("arguments", "")))
            for call in (message.get("tool_calls") or ())
            if isinstance(call, Mapping)
        )
        usage = {k: v for k, v in (payload.get("usage") or {}).items() if k in {"prompt_tokens", "completion_tokens", "total_tokens"} and isinstance(v, int)}
        return ModelResponse(message.get("content"), calls, choice.get("finish_reason"), payload.get("model"), usage)
