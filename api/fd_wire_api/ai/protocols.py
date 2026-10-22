"""构建循环的两个核心协议：``LlmClient`` 与 ``ToolExecutor``。

循环（loop.py）只依赖这两个协议——生产实现分别是
:class:`~fd_wire_api.ai.llm.TokenGatewayLlmClient`（token 网关 OpenAI 兼容面）
与 :class:`~fd_wire_api.ai.executor.CellMcpExecutor`（企业 cell MCP 端点），
测试全部以内存假实现注入，绝不打真网。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolCall:
    """LLM 发起的一次工具调用（OpenAI tool_calls 形态的归一化）。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LlmAssistantMessage:
    """LLM 一次回复的归一化形态：文本终答，或若干工具调用（或兼有）。"""

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def text(self) -> str:
        """入库文本：纯工具调用轮可为空串，终答轮为自然语言。"""
        return self.content or ""


@runtime_checkable
class LlmClient(Protocol):
    """LLM 出口协议：messages（OpenAI 形态）+ tools schema → assistant 消息。"""

    def complete(self, messages: list[dict], tools: list[dict]) -> LlmAssistantMessage:
        ...


@runtime_checkable
class ToolExecutor(Protocol):
    """工具执行协议：cell 侧探索/物化工具的执行出口。

    计量归属：实现方必须走**平台服务凭据**（AI 探索不计费——真正计量
    发生在 registry audit 侧，凭据是平台身份，不出现在客户账单）。
    """

    def execute(self, tool_name: str, args: dict) -> dict:
        ...
