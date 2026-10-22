"""LlmClient 生产实现：token 网关的 OpenAI 兼容 chat/completions 面。

env 契约：``TOKEN_GATEWAY_URL`` / ``TOKEN_GATEWAY_KEY`` /
``TOKEN_GATEWAY_MODEL``。httpx 客户端可注入（测试全 mock，不打真网）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .protocols import LlmAssistantMessage, ToolCall
from .settings import get_settings


class LlmGatewayError(RuntimeError):
    """token 网关不可达 / 非 2xx / 响应不合契约（循环侧如实反馈）。"""


class TokenGatewayLlmClient:
    """LlmClient 生产实现：``POST {TOKEN_GATEWAY_URL}/v1/chat/completions``。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        http_client: httpx.Client | None = None,
        timeout: float = 60.0,
    ) -> None:
        s = get_settings()
        self._base = (base_url or s.token_gateway_url).rstrip("/")
        self._api_key = api_key if api_key is not None else s.token_gateway_key
        self._model = model or s.token_gateway_model
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def complete(
        self, messages: list[dict], tools: list[dict]
    ) -> LlmAssistantMessage:
        """一次补全：messages + tools（OpenAI 形态）→ 归一化 assistant 消息。"""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            resp = self._http.post(
                f"{self._base}/v1/chat/completions", json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise LlmGatewayError(f"token 网关不可达: {exc}") from exc
        if resp.status_code >= 400:
            raise LlmGatewayError(
                f"token 网关返回 {resp.status_code}: {resp.text[:200]}"
            )
        try:
            body = resp.json()
            message = body["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LlmGatewayError("token 网关响应不合 OpenAI 契约") from exc

        tool_calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            try:
                fn = raw["function"]
                arguments = json.loads(fn.get("arguments") or "{}")
            except (KeyError, TypeError, ValueError) as exc:
                raise LlmGatewayError("tool_calls 形态不合契约") from exc
            if not isinstance(arguments, dict):
                raise LlmGatewayError("tool_calls arguments 必须是 JSON 对象")
            tool_calls.append(
                ToolCall(id=str(raw.get("id") or ""), name=str(fn["name"]), arguments=arguments)
            )
        content = message.get("content")
        return LlmAssistantMessage(
            content=str(content) if content is not None else None,
            tool_calls=tuple(tool_calls),
        )
