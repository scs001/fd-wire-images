"""doc（富文本文档）产物的结构层校验（wire-rich-text-docs 任务 1.2）。

与 form_spec / record_view_spec 同范式：本模块只管 **kind 内部结构**（doc 的
``spec.spec`` 那层）；通用信封（title / spec 是非空对象）由
``service.validate_spec`` 负责。

正文契约（design D1）：**Markdown 源文本是唯一事实源**——``format`` 恒为
``"markdown"``，``text`` 是源文本；不存预渲染 HTML。存 HTML 会把 XSS 面从
「唯一渲染器」扩散到「库内数据一片」，消毒就必须在下游每个消费点重复一遍
（design D2）。

体积上限（任务 1.2）：正文是产物体系里第一个「可长」载荷（既有结构化 spec
都是小 JSON），没有上限等于给库与请求体留一个无界写入口。上限可配
（``FD_WIRE_DOC_MAX_BYTES``），缺省 256 KB——对报告/说明书量级（约十几万
字）充裕，超限 422 并载明实际上限。
"""

from __future__ import annotations

import os

#: doc 正文的唯一格式标记（v1 只有 Markdown；将来加格式走新增值，不改语义）
DOC_FORMAT = "markdown"

ENV_MAX_BYTES = "FD_WIRE_DOC_MAX_BYTES"
DEFAULT_MAX_BYTES = 256 * 1024  # 256 KB

#: 空白创建的最小 spec（浏览器端创建入口与后端保持同形）
BLANK_DOC_INNER: dict = {"format": DOC_FORMAT, "text": ""}


def doc_max_bytes() -> int:
    """正文体积上限（字节，按 UTF-8 计）。配错回退缺省，不因配置问题炸。"""
    raw = os.environ.get(ENV_MAX_BYTES)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES


def validate_doc_text(text: object) -> str | None:
    """正文本身的校验（不含 format）——草稿层 PUT 复用这一条（任务 2.2），
    保证「保存版本」与「自动保存草稿」对正文的口径完全一致。"""
    if not isinstance(text, str):
        return "spec.spec.text 必须是字符串（Markdown 源文本）"
    if len(text.encode("utf-8")) > doc_max_bytes():
        return f"正文超过上限 {doc_max_bytes()} 字节（Markdown 源文本按 UTF-8 计）"
    return None


def validate_doc_spec(inner: object) -> str | None:
    """doc 的 ``spec.spec`` 校验：``{format:"markdown", text}``。

    违规返回中文错误串（与 form/record-view 校验器同契约），合法返回 None。
    """
    if not isinstance(inner, dict):
        return "spec.spec 必须是 JSON 对象"
    if inner.get("format") != DOC_FORMAT:
        return f'spec.spec.format 必须是 "{DOC_FORMAT}"'
    return validate_doc_text(inner.get("text"))
