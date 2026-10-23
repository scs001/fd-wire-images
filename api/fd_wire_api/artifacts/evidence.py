"""产物证据块（wire-info-stream-program 4.1/4.2/4.3）校验。

证据挂在产物**信封**上（``{"title", "spec", "evidence"}``），不塞进 kind 内部——
看板/报告/表单等所有 kind 共用一套校验，且既有产物不带 evidence 时零影响
（向后兼容硬约束：无证据照常通过）。

两层校验 + 一个发布门：

- 保存层（``validate_evidence``）：纯结构校验，离线可跑；错误定位到
  ``evidence[索引].字段``。
- 发布层（``verify_evidence``）：逐条核对 info-stream——条目存在性
  （``source``+``stable_id`` 命中）、逐字片段（正文按 span 区间服务端复核）、
  时间（``as_of`` 不得晚于产物版本时间）。
- 发布门（``EvidenceGateSettings``）：``EVIDENCE_GATE_MODE=off|warn|enforce``，
  默认 **warn**——校验失败只记结构化告警日志不阻断发布；观察一个发布周期
  无误伤后再切 enforce（422）。off 完全跳过。

fd-info 服务尚未部署：发布层经窄适配器 :protocol:`InfoStore` 访问
（``resolve(source, stable_id)``），生产实现后续接 fd-info 查询面
（``HttpInfoStore``，由 ``FD_INFO_URL`` 指向）；测试注入假实现。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

EVIDENCE_KEY = "evidence"
SPAN_KEYS = ("start", "end", "quote")

#: 发布门三态（design D5：灰度先 warn，观察一个周期后 enforce）
GATE_OFF = "off"
GATE_WARN = "warn"
GATE_ENFORCE = "enforce"
GATE_MODES = (GATE_OFF, GATE_WARN, GATE_ENFORCE)


class EvidenceError(ValueError):
    """证据块结构不合法；文案定位到具体证据项（索引/字段名）。"""


# ------------------------------------------------------------ 结构层（保存）

@dataclass(frozen=True)
class EvidenceItem:
    source: str
    stable_id: str
    as_of: str
    start: int
    end: int
    quote: str
    claim: str | None = None


def _is_iso_date(value: object) -> bool:
    if not isinstance(value, str) or len(value) < 10:
        return False
    text = value[:10]
    return text[4] == "-" and text[7] == "-" and text.replace("-", "").isdigit()


def parse_evidence(evidence: object) -> list[EvidenceItem]:
    """解析并结构校验 evidence 数组（非法抛 EvidenceError，文案带索引+字段）。"""
    if evidence is None:
        return []
    if not isinstance(evidence, list):
        raise EvidenceError("evidence 必须是数组")
    items: list[EvidenceItem] = []
    for index, raw in enumerate(evidence):
        where = f"evidence[{index}]"
        if not isinstance(raw, dict):
            raise EvidenceError(f"{where} 必须是 JSON 对象")
        for key in ("source", "stable_id", "as_of", "span"):
            if key not in raw:
                raise EvidenceError(f"{where} 缺少 {key}")
        if not isinstance(raw["source"], str) or not raw["source"].strip():
            raise EvidenceError(f"{where}.source 必须是非空字符串")
        if not isinstance(raw["stable_id"], str) or not raw["stable_id"].strip():
            raise EvidenceError(f"{where}.stable_id 必须是非空字符串")
        if not _is_iso_date(raw["as_of"]):
            raise EvidenceError(f"{where}.as_of 必须是 ISO 日期（YYYY-MM-DD）")
        span = raw["span"]
        if not isinstance(span, dict):
            raise EvidenceError(f"{where}.span 必须是 JSON 对象")
        for key in SPAN_KEYS:
            if key not in span:
                raise EvidenceError(f"{where}.span 缺少 {key}")
        start, end, quote = span["start"], span["end"], span["quote"]
        if isinstance(start, bool) or not isinstance(start, int):
            raise EvidenceError(f"{where}.span.start 必须是整数")
        if isinstance(end, bool) or not isinstance(end, int):
            raise EvidenceError(f"{where}.span.end 必须是整数")
        if start < 0 or end <= start:
            raise EvidenceError(f"{where}.span 区间非法：{start}..{end}")
        if not isinstance(quote, str) or not quote:
            raise EvidenceError(f"{where}.span.quote 必须是非空字符串")
        claim = raw.get("claim")
        if claim is not None and not isinstance(claim, str):
            raise EvidenceError(f"{where}.claim 必须是字符串")
        items.append(
            EvidenceItem(
                source=raw["source"].strip(),
                stable_id=raw["stable_id"].strip(),
                as_of=raw["as_of"],
                start=start,
                end=end,
                quote=quote,
                claim=claim,
            )
        )
    return items


def validate_evidence(spec: object) -> str | None:
    """保存层结构校验（None=合法）；供 ``validate_kind_spec`` 信封级追加。"""
    if not isinstance(spec, dict):
        return None
    try:
        parse_evidence(spec.get(EVIDENCE_KEY))
    except EvidenceError as exc:
        return str(exc)
    return None


# ------------------------------------------------- info-store 适配器（窄口）

@dataclass(frozen=True)
class ResolvedEvidence:
    """info-store 命中结果：正文（doc 全文或 chunk 文本）+ 条目发布时间。

    ``text=None`` 表示条目存在但正文不可用（body_status≠stored 等）——
    存在性算过，逐字片段无法复核。
    """

    text: str | None
    published_at: str | None = None


class InfoStore(Protocol):
    """info-stream 只读窄口（fd-info 查询面的可注入适配器）。

    ``resolve`` 返回 None 表示 ``(source, stable_id)`` 未在 info_items 中
    命中（伪造 stable_id / 来源不符同此口径）；异常向上抛（由校验层转为
    可见失败，绝不静默放行）。
    """

    def resolve(self, *, source: str, stable_id: str) -> ResolvedEvidence | None: ...


# ------------------------------------------------------- 发布层（逐条核对）

@dataclass(frozen=True)
class EvidenceFailure:
    """一条证据核对失败：定位到 ``evidence[index].field`` + 人读原因。"""

    index: int  # -1 = 块级（结构/服务未配置）
    field: str  # source / stable_id / span / as_of / info_store / evidence
    reason: str
    source: str = ""
    stable_id: str = ""

    def message(self) -> str:
        if self.index < 0:
            return f"evidence.{self.field}：{self.reason}"
        return f"evidence[{self.index}].{self.field}：{self.reason}"


def verify_evidence(
    spec: object,
    store: InfoStore | None,
    *,
    version_time: str | None = None,
) -> list[EvidenceFailure]:
    """发布层逐条核对（返回失败清单；空列表=全部通过或无证据）。

    三项核对（design D5）：

    1. 存在性：``resolve(source, stable_id)`` 未命中；
    2. 逐字片段：正文按 ``span`` 区间**服务端复核**——``text[start:end]``
       必须与 ``quote`` 完全一致（含区间越界）；
    3. 时间：``as_of`` 不得晚于产物版本时间（``version_time``，ISO）。
    """
    if not isinstance(spec, dict):
        return []
    try:
        items = parse_evidence(spec.get(EVIDENCE_KEY))
    except EvidenceError as exc:
        return [EvidenceFailure(index=-1, field="evidence", reason=str(exc))]
    if not items:
        return []
    if store is None:
        return [
            EvidenceFailure(
                index=-1,
                field="info_store",
                reason="证据校验服务未配置（FD_INFO_URL），无法核对 info-stream",
            )
        ]
    failures: list[EvidenceFailure] = []
    for index, item in enumerate(items):
        if version_time and item.as_of > version_time[:10]:
            failures.append(
                EvidenceFailure(
                    index=index,
                    field="as_of",
                    reason=f"as_of（{item.as_of}）晚于产物版本时间（{version_time[:10]}）",
                    source=item.source,
                    stable_id=item.stable_id,
                )
            )
        try:
            resolved = store.resolve(source=item.source, stable_id=item.stable_id)
        except Exception as exc:  # 适配器故障（网络等）转为可见失败，不放行不崩溃
            failures.append(
                EvidenceFailure(
                    index=index,
                    field="source",
                    reason=f"info-store 查询失败：{exc}",
                    source=item.source,
                    stable_id=item.stable_id,
                )
            )
            continue
        if resolved is None:
            failures.append(
                EvidenceFailure(
                    index=index,
                    field="stable_id",
                    reason=f"条目不存在：{item.source}/{item.stable_id} 未在 info_items 中命中",
                    source=item.source,
                    stable_id=item.stable_id,
                )
            )
            continue
        if resolved.text is None:
            failures.append(
                EvidenceFailure(
                    index=index,
                    field="span",
                    reason="条目正文不可用（body 未落库），无法复核逐字片段",
                    source=item.source,
                    stable_id=item.stable_id,
                )
            )
            continue
        text = resolved.text
        if item.end > len(text):
            failures.append(
                EvidenceFailure(
                    index=index,
                    field="span",
                    reason=f"区间越界：end={item.end} 超出正文长度 {len(text)}",
                    source=item.source,
                    stable_id=item.stable_id,
                )
            )
        elif text[item.start : item.end] != item.quote:
            failures.append(
                EvidenceFailure(
                    index=index,
                    field="span",
                    reason=(
                        f"quote 与正文 [{item.start}:{item.end}] 逐字不一致"
                        f"（正文该区间为 {text[item.start : item.end]!r}）"
                    ),
                    source=item.source,
                    stable_id=item.stable_id,
                )
            )
    return failures


# ----------------------------------------------------------- 发布门（4.3）

class EvidenceGateSettings(BaseSettings):
    """证据发布门配置（照 schedules/settings 范式：同名 env 优先）。

    ``EVIDENCE_GATE_MODE=off|warn|enforce``，默认 **warn**（tasks 4.3：
    先仅告警观察一个发布周期）；非法值回落 warn 并记告警（不炸启动）。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mode: str = Field(
        default=GATE_WARN,
        validation_alias="EVIDENCE_GATE_MODE",
    )

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        mode = (value or "").strip().lower()
        if mode not in GATE_MODES:
            logger.warning(
                "EVIDENCE_GATE_MODE=%r 非法（合法值 %s），回落 warn",
                value,
                "|".join(GATE_MODES),
            )
            return GATE_WARN
        return mode


@lru_cache(maxsize=1)
def get_evidence_gate_settings() -> EvidenceGateSettings:
    """进程内单例；测试重设 env 后 ``cache_clear()`` 再取。"""
    return EvidenceGateSettings()


# ------------------------------------------- 生产适配器（fd-info 查询面）

#: 探测正文的超长右端（fd-info 的 locate 对越界报"正文 N 字"，借此取全文）
_PROBE_END = 1 << 24
_BODY_LENGTH_RE = re.compile(r"正文\s*(\d+)\s*字")


class HttpInfoStore:
    """经 fd-info MCP 查询面取条目与正文（生产实现；fd-info 部署后启用）。

    ``resolve`` 语义：get_item 未命中/来源不符 → None；正文未落库 →
    ``ResolvedEvidence(text=None)``；正文经 locate_evidence 以
    ``[0, 正文长度)`` 取回全文（fd-info 暂无整文工具，用超长区间探长度后
    全量取——探测不出长度按正文不可用处理，绝不伪造）。任何上游异常向上
    抛，由 :func:`verify_evidence` 转为定位到证据项的可见失败。
    """

    PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

    def __init__(self, url: str, token: str | None = None) -> None:
        self.url = url
        self.token = token
        self._rpc_id = 0

    def resolve(self, *, source: str, stable_id: str) -> ResolvedEvidence | None:
        item = self._call("get_item", {"item_id": stable_id})
        if not item or item.get("error"):
            return None
        if item.get("source") != source:
            # (source, stable_id) 身份不匹配＝未命中（存在性按对校验）
            return None
        published_at = item.get("published_at")
        doc_id = item.get("doc_id")
        if item.get("body_status") != "stored" or not doc_id:
            return ResolvedEvidence(text=None, published_at=published_at)
        return ResolvedEvidence(
            text=self._fetch_text(str(doc_id)),
            published_at=published_at,
        )

    def _fetch_text(self, doc_id: str) -> str | None:
        located = self._call("locate_evidence", {"doc_id": doc_id, "start": 0, "end": _PROBE_END})
        if located.get("ok") is False:
            match = _BODY_LENGTH_RE.search(str(located.get("error") or ""))
            if match is None:
                return None
            located = self._call(
                "locate_evidence", {"doc_id": doc_id, "start": 0, "end": int(match.group(1))}
            )
        if located.get("ok") is False or not isinstance(located.get("quote"), str):
            return None
        return str(located["quote"])

    # ----------------------------------------------------------- MCP 传输

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        import httpx

        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            session = None
            for version in self.PROTOCOLS:
                self._rpc_id += 1
                resp = client.post(
                    self.url,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": self._rpc_id,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": version,
                            "capabilities": {},
                            "clientInfo": {"name": "fd-wire-api", "version": "1.0"},
                        },
                    },
                )
                if resp.status_code == 400 and "protocol" in resp.text.lower():
                    continue
                resp.raise_for_status()
                session = resp.headers.get("mcp-session-id")
                if not session:
                    raise ValueError("initialize 未返回 Mcp-Session-Id")
                client.post(
                    self.url,
                    headers={**headers, "Mcp-Session-Id": session},
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                break
            if not session:
                raise ValueError("所有协议版本都被拒绝")
            self._rpc_id += 1
            resp = client.post(
                self.url,
                headers={**headers, "Mcp-Session-Id": session},
                json={
                    "jsonrpc": "2.0",
                    "id": self._rpc_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            resp.raise_for_status()
            payload = _decode(resp)
            if "error" in payload:
                raise ValueError(str(payload["error"]))
            return _payload(payload)


def _decode(resp) -> dict[str, Any]:
    text = resp.text.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise ValueError("响应既不是 JSON 也不是 SSE")


def _payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result") or {}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        # fastmcp 对 dict 返回可能再包一层 result
        inner = structured.get("result")
        return inner if isinstance(inner, dict) else structured
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                return json.loads(block.get("text") or "")
            except json.JSONDecodeError:
                continue
    return {}


def info_store_from_env() -> InfoStore | None:
    """生产适配器装配点：``FD_INFO_URL`` 未设 → None（校验层按未配置处理）。"""
    url = os.environ.get("FD_INFO_URL")
    if not url:
        return None
    return HttpInfoStore(url, token=os.environ.get("FD_INFO_TOKEN"))
