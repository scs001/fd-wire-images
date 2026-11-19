"""``tools/call`` 结果解包（从 datasets/ingest._unwrap 提炼，gateway/native 共用）。

兼容形态（按序尝试，全部有测试钉住）：

1. JSON-RPC error 对象（``{"error": {...}}``）→ :class:`McpCallError`；
2. ``{"result": {...}}``（JSON-RPC 成功信封）→ MCP 工具结果：
   ``structuredContent`` 优先，``content`` 文本块 JSON 兜底；
3. ``{"success": ...}`` 业务信封 → 整体即结果；
4. 其他 JSON 对象 → 直接业务 dict（gateway 简化形态的直答）。

结果内层再按 MCP 语义校验：``isError: true`` / ``success: false`` →
:class:`McpCallError`（工具自身报错）；``success: true`` 信封剥掉，调用方
只见业务字段。非 JSON 对象（数组/标量）→ :class:`McpCallError`。
"""

from __future__ import annotations

import json
from typing import Any

from .errors import McpCallError


def unwrap_tool_result(payload: Any) -> dict:
    """从 MCP 响应取工具结果 dict；任何不合契约都抛 :class:`McpCallError`。"""
    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise McpCallError(f"返回 JSON-RPC 错误: {message}")
    result = payload.get("result") if isinstance(payload, dict) else None
    if result is None and isinstance(payload, dict) and "success" in payload:
        result = payload
    if result is None and isinstance(payload, dict):
        # 直接业务 dict（gateway 简化形态直答，无任何信封）
        result = payload
    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            result = structured
        elif isinstance(result.get("content"), list):
            texts = [
                item.get("text", "")
                for item in result["content"]
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            joined = "".join(texts).strip()
            if not joined:
                raise McpCallError("响应缺少结果内容")
            try:
                result = json.loads(joined)
            except ValueError as exc:
                raise McpCallError("结果非 JSON") from exc
    if not isinstance(result, dict):
        raise McpCallError("响应形态不合契约（需 JSON 对象）")
    if result.get("isError") is True:
        raise McpCallError(f"工具报错: {result.get('error') or result}")
    if result.get("success") is False:
        raise McpCallError(f"工具报错: {result.get('error') or result}")
    if result.get("success") is True:
        # 引擎成功信封统一剥掉，调用方只见业务字段
        return {key: value for key, value in result.items() if key != "success"}
    return result


__all__ = ["unwrap_tool_result"]
