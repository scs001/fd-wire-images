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

    def complete_stream(
        self,
        messages: list[dict],
        tools: list[dict],
        on_delta,
    ) -> LlmAssistantMessage:
        """流式补全（assistant-streaming D6）：增量文本经 ``on_delta`` 回调。

        返回值与 :meth:`complete` **同形**——循环侧不需要区分两条路径。
        ``on_delta(text)`` 收到的是**累加后的全文**，不是片段。

        网关对 ``stream: true`` 的 tool_calls 分片支持未经实测（design D6 风险）：
        任何分片解析失败都退回本轮非流式补全——只损失这一轮的打字效果，
        正确性不受影响。
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "stream": True,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

        acc = ""
        # index → {id, name, arguments 分片缓冲}
        calls: dict[int, dict[str, Any]] = {}
        try:
            with self._http.stream(
                "POST",
                f"{self._base}/v1/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status_code >= 400:
                    raise LlmGatewayError(f"token 网关返回 {resp.status_code}")
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if not body or body == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(body)
                        delta = chunk["choices"][0]["delta"]
                    except (ValueError, KeyError, IndexError, TypeError):
                        continue  # 单帧不合契约：跳过，不打断整轮
                    piece = delta.get("content")
                    if isinstance(piece, str) and piece:
                        acc += piece
                        on_delta(acc)
                    for raw in delta.get("tool_calls") or []:
                        try:
                            idx = int(raw.get("index", 0))
                        except (TypeError, ValueError):
                            idx = 0
                        slot = calls.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""}
                        )
                        if raw.get("id"):
                            slot["id"] = str(raw["id"])
                        fn = raw.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = str(fn["name"])
                        if isinstance(fn.get("arguments"), str):
                            slot["arguments"] += fn["arguments"]
        except httpx.HTTPError as exc:
            raise LlmGatewayError(f"token 网关不可达: {exc}") from exc

        tool_calls: list[ToolCall] = []
        for idx in sorted(calls):
            slot = calls[idx]
            if not slot["name"]:
                continue
            try:
                arguments = json.loads(slot["arguments"] or "{}")
            except ValueError:
                # 分片拼接不成合法 JSON：退回非流式补全本轮（design D6 风险缓解）。
                return self.complete(messages, tools)
            if not isinstance(arguments, dict):
                return self.complete(messages, tools)
            tool_calls.append(
                ToolCall(
                    id=slot["id"] or f"call-{idx}",
                    name=slot["name"],
                    arguments=arguments,
                )
            )
        return LlmAssistantMessage(content=acc or None, tool_calls=tuple(tool_calls))
