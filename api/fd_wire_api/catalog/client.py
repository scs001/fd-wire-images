"""权威源客户端：统一指标注册目录（fd-open-data-mcp 侧 ``list_registry_entries``）。

- ``RegistryClient`` 是协议（Protocol）：测试用内存假实现替换，生产用
  ``HttpRegistryClient``。同步器只依赖协议，不依赖 HTTP 细节。
- 权威源是 fd-open-data-mcp 的 MCP streamable-http 端点（fastmcp 3.4.4 /
  mcp 1.28.1，实测线格式 2026-10）：
  1. POST ``initialize``（JSON-RPC 2.0）→ 200，SSE 或 JSON 体携带 result，
     响应头 ``Mcp-Session-Id`` 是后续请求的会话凭证；
  2. POST ``notifications/initialized``（202 无体）；
  3. POST ``tools/call`` ``list_registry_entries``，参数 ``limit``/``offset``
     （服务端 clamp 至 [1,1000]），「页短于 limit 即末页」翻页——无 cursor、
     无 total；结果在 ``structuredContent.result``（fastmcp 对 list 返回的
     包装）或 ``content[0].text``（JSON 文本）。
- 工具契约（固定）：``list_registry_entries(status=None, limit=500,
  offset=0)``——``status`` 缺省=全量（verified+registered）；行字段
  ``source_db, native_code, semantic_code, name_zh, name_en, unit,
  frequency, domain, verified``（``verified`` 为 bool）。全量枚举含未核验
  条目（这是换源的目的：目录浏览面呈现全部登记条目）。
- 标识方案：``indicator_id = f"{source_db}:{native_code}"``——注册表唯一
  锚本是（来源库, 原生码）；语义码跨源等价可重复，不得当主键。语义码、
  双语名、单位、频率、域一律作为条目属性（``code``/``name_*``/``unit``/
  ``frequency``/``domain``），不进标识。
- 无增量游标：``list_registry_entries`` **没有** ``updated_since`` 参数，
  客户端每次全量翻页，幂等 upsert 兜底（沿用现契约）。
- 凭据：``INDICATOR_REGISTRY_TOKEN`` 以 Bearer 随每个请求上行（公网入口
  网关鉴权）；空值 = 匿名。
- ``INDICATOR_REGISTRY_URL`` 指权威基地址（如
  ``https://mcp.finddatatech.cloud/fd-open-data-mcp``），``/mcp`` 后缀由
  客户端自动补齐（已带则不重复）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from .settings import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


@dataclass
class ConceptRecord:
    """规范化后的权威库指标记录（展示字段 + 变更时间）。

    ``indicator_id = source_db:native_code``（全局唯一稳定主键）；``code``
    承载语义码（跨源可重复，仅作展示/搜索属性）。
    """

    indicator_id: str
    source_db: str | None = None
    native_code: str | None = None
    code: str = ""
    name_zh: str | None = None
    name_en: str | None = None
    description_zh: str | None = None
    description_en: str | None = None
    domain: str | None = None
    frequency: str | None = None
    unit: str | None = None
    time_span_start: str | None = None
    time_span_end: str | None = None
    entity_coverage_count: int | None = None
    sample_value: str | None = None
    status: str = "registered"  # verified | registered，只来自权威库
    updated_at: datetime | None = None


@dataclass
class ConceptBatch:
    """一次拉取的结果：记录集合 + 水位（无行级 updated_at 时为拉取时刻）。"""

    items: list[ConceptRecord] = field(default_factory=list)
    next_watermark: datetime | None = None
    full: bool = False


class RegistryClient(Protocol):
    """权威源客户端协议（可 mock）。"""

    def fetch_concepts(self) -> ConceptBatch:
        ...  # pragma: no cover


def _first_str(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    return None


def _first_int(raw: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def normalize_status(value: Any) -> str:
    """权威状态归一：只允许 verified | registered（保守落 registered）。"""
    text = str(value or "").strip().lower()
    return "verified" if text == "verified" else "registered"


def _row_verified(row: dict[str, Any]) -> bool:
    """登记行的 ``verified`` 段：契约是 bool；字符串 "true"/"false" 容错。"""
    verified = row.get("verified")
    if verified is True:
        return True
    if isinstance(verified, str) and verified.strip().lower() == "true":
        return True
    return False


# indicator_id 列宽（indicator_snapshots.indicator_id String(128)）；超长时
# 退化为前缀 + sha1 后缀（确定性、无截断碰撞）。
_INDICATOR_ID_MAX_LEN = 128


def _stable_indicator_id(composite: str) -> str:
    if len(composite) <= _INDICATOR_ID_MAX_LEN:
        return composite
    digest = hashlib.sha1(composite.encode("utf-8")).hexdigest()[:7]
    return f"{composite[:120]}~{digest}"


def normalize_record(raw: dict[str, Any]) -> ConceptRecord:
    """把权威 ``list_registry_entries`` 行规范化为 ConceptRecord。

    - ``indicator_id`` ＝ ``source_db:native_code``：全局唯一稳定主键——
      名称/单位/语义码等展示字段被权威库修正时不变，跨源等价条目
      （语义码相同、source_db 不同）不碰撞；
    - ``code`` ← ``semantic_code``（语义码，展示/搜索属性，非主键）；
    - name_zh/name_en/unit/frequency/domain 直映；
    - ``status`` ← ``verified``（两级口径：verified | registered）。
    """
    source_db = _first_str(raw, "source_db")
    native_code = _first_str(raw, "native_code")
    if not source_db or not native_code:
        raise ValueError(
            f"registry entry without usable source_db/native_code: {raw!r}"
        )
    return ConceptRecord(
        indicator_id=_stable_indicator_id(f"{source_db}:{native_code}"),
        source_db=source_db,
        native_code=native_code,
        code=_first_str(raw, "semantic_code") or "",
        name_zh=_first_str(raw, "name_zh"),
        name_en=_first_str(raw, "name_en"),
        description_zh=_first_str(raw, "description_zh"),
        description_en=_first_str(raw, "description_en"),
        domain=_first_str(raw, "domain"),
        frequency=_first_str(raw, "frequency"),
        unit=_first_str(raw, "unit"),
        status="verified" if _row_verified(raw) else "registered",
        updated_at=_parse_dt(raw.get("updated_at") or raw.get("updated")),
    )


_LIST_KEYS = ("items", "data", "concepts", "results", "records")


def _extract_items(payload: Any) -> tuple[list[Any], str | None]:
    """从权威响应中提取记录列表与 next cursor（别名容错）。"""
    next_cursor: str | None = None
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        next_cursor = payload.get("next_cursor") or payload.get("cursor") or payload.get("next")
        for key in _LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value, next_cursor
        # 单条对象本身即记录
        if any(k in payload for k in ("indicator_id", "native_code", "source_db", "id")):
            return [payload], None
    raise ValueError(f"unexpected authority payload shape: {type(payload)!r}")


# ─── MCP streamable-http 线协议（与 fd-open-data-mcp server.py 对齐） ────────

_MCP_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_SESSION_HEADER = "Mcp-Session-Id"
_LIST_TOOL = "list_registry_entries"
_TOOL_LIST_KEYS = ("result", "items", "data", "concepts", "results", "records")


def _mcp_endpoint(base_url: str) -> str:
    """归一权威基地址为 streamable-http 端点：确保以 ``/mcp`` 结尾。"""
    trimmed = (base_url or "").rstrip("/")
    if trimmed.endswith("/mcp"):
        return trimmed
    return f"{trimmed}/mcp"


def _decode_rpc_payload(resp: httpx.Response) -> dict[str, Any]:
    """解码 JSON-RPC 响应体：streamable-http 以 SSE（``data:`` 行）或纯 JSON 应答。"""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    payload = json.loads(line[len("data:"):].strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and ("result" in payload or "error" in payload):
                    return payload
        raise ValueError(
            f"authority SSE response carried no JSON-RPC message: {resp.text[:200]!r}"
        )
    return resp.json()


def _raise_on_rpc_error(payload: dict[str, Any], context: str) -> None:
    error = payload.get("error")
    if isinstance(error, dict):
        raise RuntimeError(
            f"authority {context} RPC error {error.get('code')}: {error.get('message')}"
        )


def _tool_result_rows(payload: dict[str, Any], context: str = "tools/call") -> list[Any]:
    """从 ``tools/call`` 结果提取行集合：``structuredContent`` 优先，text 块兜底。"""
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"authority {context} returned no result object: {payload!r}")
    if result.get("isError"):
        texts = [
            block.get("text", "")
            for block in result.get("content") or []
            if isinstance(block, dict)
        ]
        detail = " ".join(t for t in texts if t) or repr(result)
        raise RuntimeError(f"authority {context} tool error: {detail}")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        for key in _TOOL_LIST_KEYS:
            if isinstance(structured.get(key), list):
                return structured[key]
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            try:
                parsed = json.loads(block["text"])
            except json.JSONDecodeError:
                continue
            try:
                items, _cursor = _extract_items(parsed)
            except ValueError:
                continue
            return items
    raise ValueError(f"authority {context} carried no row payload")


class HttpRegistryClient:
    """MCP streamable-http 权威源客户端（fd-open-data-mcp 的
    ``list_registry_entries``，登记表全量枚举）。

    ``transport`` 可注入（httpx.MockTransport）供全 mock 测试；生产缺省走
    真实网络。凭据 ``token`` 缺省取 ``settings.indicator_registry_token``
    （env ``INDICATOR_REGISTRY_TOKEN``），非空即随每个请求上行 Bearer。
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
        page_size: int = 500,
        max_pages: int = 1000,
        transport: httpx.BaseTransport | None = None,
    ):
        self._base_url = _mcp_endpoint(base_url or settings.indicator_registry_url)
        effective_token = token if token is not None else settings.indicator_registry_token
        self._token = (effective_token or "").strip()
        self._timeout = timeout
        self._page_size = max(1, min(int(page_size), 1000))
        self._max_pages = max(1, int(max_pages))
        self._transport = transport
        self._rpc_id = 0

    # ── 线协议 plumbing ─────────────────────────────────────────────────────

    def _post(
        self,
        client: httpx.Client,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if session_id:
            headers[_SESSION_HEADER] = session_id
        return client.post(self._base_url, headers=headers, json=payload)

    def _initialize(self, client: httpx.Client) -> str:
        """initialize 握手（协议版本按序试探），返回 ``Mcp-Session-Id``。"""
        for version in _MCP_PROTOCOL_VERSIONS:
            self._rpc_id += 1
            resp = self._post(
                client,
                {
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
                continue  # 服务端不支持该协议版本 → 试下一档
            resp.raise_for_status()
            payload = _decode_rpc_payload(resp)
            _raise_on_rpc_error(payload, "initialize")
            session_id = resp.headers.get("mcp-session-id")
            if not session_id:
                raise ValueError("authority MCP initialize returned no Mcp-Session-Id header")
            # initialized 通知（无 id；服务端 202 无体）
            note = self._post(
                client,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                session_id=session_id,
            )
            if note.status_code >= 400:
                raise RuntimeError(
                    f"authority MCP initialized notification rejected: {note.status_code}"
                )
            return session_id
        raise ValueError("authority MCP rejected every known protocol version")

    def _call_list_tool(
        self, client: httpx.Client, session_id: str, offset: int
    ) -> list[Any]:
        """``tools/call list_registry_entries``：limit/offset 翻页（status 缺省=全量）。"""
        self._rpc_id += 1
        resp = self._post(
            client,
            {
                "jsonrpc": "2.0",
                "id": self._rpc_id,
                "method": "tools/call",
                "params": {
                    "name": _LIST_TOOL,
                    "arguments": {"limit": self._page_size, "offset": offset},
                },
            },
            session_id=session_id,
        )
        resp.raise_for_status()
        payload = _decode_rpc_payload(resp)
        _raise_on_rpc_error(payload, f"{_LIST_TOOL} (offset={offset})")
        return _tool_result_rows(payload, context=f"{_LIST_TOOL} (offset={offset})")

    def _close_session(self, client: httpx.Client, session_id: str) -> None:
        """尽力关闭 MCP 会话（streamable-http DELETE）；失败不影响结果。"""
        try:
            headers = {_SESSION_HEADER: session_id}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            client.delete(self._base_url, headers=headers)
        except Exception:  # pragma: no cover - 清理失败可忽略
            pass

    # ── RegistryClient 协议实现 ─────────────────────────────────────────────

    def fetch_concepts(self) -> ConceptBatch:
        """全量翻页拉取登记表（无增量游标：每次都是全量，幂等 upsert 兜底）。"""
        pulled: list[ConceptRecord] = []
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            session_id = self._initialize(client)
            try:
                offset = 0
                # 保守分页上限，防权威侧异常翻页不终止
                for _ in range(self._max_pages):
                    page = self._call_list_tool(client, session_id, offset)
                    for row in page:
                        # 契约行必须携带 source_db/native_code；畸形行 fail-fast
                        # （同步器整批中止、旧镜像原样保留，见 sync 的异常路径）
                        if isinstance(row, dict):
                            pulled.append(normalize_record(row))
                    if len(page) < self._page_size:
                        break
                    offset += self._page_size
                else:  # pragma: no cover - 权威侧异常时的防御
                    raise ValueError("authority pagination did not terminate")
            finally:
                self._close_session(client, session_id)
        # 登记行不携带 updated_at（当前契约）：水位退化为拉取时刻，
        # 仅作同步状态展示，不再承担增量语义。
        return ConceptBatch(items=pulled, full=True, next_watermark=utcnow())


__all__ = [
    "ConceptBatch",
    "ConceptRecord",
    "HttpRegistryClient",
    "RegistryClient",
    "normalize_record",
    "normalize_status",
    "utcnow",
]
