"""信息流面板的确定性刷新契约（wire-analysis-console：信息流面板绑定）。

与数据集查询契约同分工：

- 面板可选 ``info_query``（``{sources?, source_types?, published_after?, limit?}``）。
- 刷新时 ≥1 面板带合法 ``info_query`` → 走本模块，逐面板调 fd-info
  ``list_since``（零 LLM），把条目映射为快照列。
- 来源不可用/上游失败 → **保留上次快照**并写 ``info_lag`` 标注，
  绝不用空列表冒充"没有新信息"。

本模块不持有数据库会话；fd-info 客户端可注入（测试用假实现）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

INFO_QUERY_KEY = "info_query"
INFO_LAG_KEY = "info_lag"

#: 列表角色 → 快照列名（与前端 list 面板 encoding 角色一致）
DEFAULT_COLUMNS = ("title", "summary", "time", "source", "link")


class InfoUnavailable(RuntimeError):
    """fd-info 不可用；调用方保留旧快照并标注滞后。"""


class InfoClient(Protocol):
    def list_items(self, query: dict[str, Any]) -> dict[str, Any]:
        """返回 ``{"items": [...]}``；失败抛 :class:`InfoUnavailable`。"""

    def get_item(self, item_id: str) -> dict[str, Any]:
        """条目详情（含 url/doc_id）；失败抛 :class:`InfoUnavailable`。"""

    def locate(self, doc_id: str, start: int, end: int) -> dict[str, Any]:
        """证据定位；失败抛 :class:`InfoUnavailable`。"""


@dataclass(frozen=True)
class InfoRefreshResult:
    panel_id: str
    status: str  # ok | lag
    count: int = 0
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "panel_id": self.panel_id,
            "status": self.status,
            "count": self.count,
            "reason": self.reason,
        }


def parse_info_query(raw: object) -> dict[str, Any] | None:
    """宽容归一：非法整体丢弃（面板照常按旧快照渲染），不报 500。"""
    if not isinstance(raw, dict):
        return None
    sources = _str_list(raw.get("sources"))
    source_types = _str_list(raw.get("source_types"))
    published_after = raw.get("published_after")
    if published_after is not None and not _is_iso_date(published_after):
        published_after = None
    limit = raw.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool):
        limit = 20
    limit = max(1, min(limit, 200))
    query: dict[str, Any] = {"limit": limit}
    if sources:
        query["sources"] = sources
    if source_types:
        query["source_types"] = source_types
    if published_after:
        query["published_after"] = published_after
    return query


def info_refresh_plan(spec: object) -> list[tuple[int, dict, dict]]:
    """提取可刷新的信息流面板：``[(index, panel, info_query)]``。"""
    panels = _panels(spec)
    plan: list[tuple[int, dict, dict]] = []
    for index, panel in enumerate(panels):
        if not isinstance(panel, dict) or panel.get("type") != "list":
            continue
        query = parse_info_query(panel.get(INFO_QUERY_KEY))
        if query is not None:
            plan.append((index, panel, query))
    return plan


def refresh_spec_with_info(
    spec: object,
    client: InfoClient | None,
    *,
    now: str,
) -> tuple[dict, list[InfoRefreshResult]]:
    """逐面板刷新信息流快照。返回 ``(new_spec, results)``。"""
    new_spec = _copy(spec)
    panels = _panels(new_spec)
    results: list[InfoRefreshResult] = []
    for index, panel, query in info_refresh_plan(new_spec):
        panel_id = str(panel.get("id") or f"panel-{index}")
        if client is None:
            _mark_lag(panel, now, "fd-info 未配置（FD_INFO_URL）")
            results.append(InfoRefreshResult(panel_id, "lag", reason="fd-info 未配置"))
            continue
        try:
            payload = client.list_items(query)
        except InfoUnavailable as exc:
            _mark_lag(panel, now, str(exc))
            results.append(InfoRefreshResult(panel_id, "lag", reason=str(exc)))
            continue
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            _mark_lag(panel, now, "fd-info 返回结构不合法")
            results.append(InfoRefreshResult(panel_id, "lag", reason="返回结构不合法"))
            continue
        panel["snapshot"] = _snapshot_from_items(panel, items)
        panel.pop(INFO_LAG_KEY, None)
        results.append(InfoRefreshResult(panel_id, "ok", count=len(items)))
    return new_spec, results


def _mark_lag(panel: dict, now: str, reason: str) -> None:
    panel[INFO_LAG_KEY] = {"at": now, "reason": reason}


def _snapshot_from_items(panel: dict, items: list[dict]) -> dict[str, list]:
    """按 list 面板角色列映射条目字段；缺角色时用固定列名兜底。"""
    encoding = panel.get("encoding") if isinstance(panel.get("encoding"), dict) else {}
    roles = {
        role: encoding.get(role) if isinstance(encoding.get(role), str) else role
        for role in DEFAULT_COLUMNS
    }
    columns = [roles[role] for role in DEFAULT_COLUMNS]
    rows: list[list[Any]] = []
    for item in items:
        rows.append(
            [
                item.get("title"),
                item.get("summary"),
                item.get("published_at"),
                item.get("source"),
                item.get("url"),
            ]
        )
    return {"columns": columns, "rows": rows}


def _panels(spec: object) -> list:
    if not isinstance(spec, dict):
        return []
    inner = spec.get("spec") if isinstance(spec.get("spec"), dict) else spec
    panels = inner.get("panels") if isinstance(inner, dict) else None
    return panels if isinstance(panels, list) else []


def _copy(spec: object) -> dict:
    return json.loads(json.dumps(spec)) if isinstance(spec, dict) else {}


def _str_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [v for v in raw if isinstance(v, str) and v]


def _is_iso_date(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    return value[4] == "-" and value[7] == "-" and value.replace("-", "").isdigit()


class HttpInfoClient:
    """fd-info MCP 的 `list_since` 薄客户端（streamable-http）。"""

    PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

    def __init__(self, url: str, token: str | None = None) -> None:
        self.url = url
        self.token = token
        self._rpc_id = 0

    def list_items(self, query: dict[str, Any]) -> dict[str, Any]:
        import httpx

        try:
            return self._call("list_since", query)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise InfoUnavailable(f"fd-info 不可用：{exc}") from exc

    def get_item(self, item_id: str) -> dict[str, Any]:
        import httpx

        try:
            return self._call("get_item", {"item_id": item_id})
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise InfoUnavailable(f"fd-info 不可用：{exc}") from exc

    def locate(self, doc_id: str, start: int, end: int) -> dict[str, Any]:
        import httpx

        try:
            return self._call(
                "locate_evidence", {"doc_id": doc_id, "start": start, "end": end}
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise InfoUnavailable(f"fd-info 不可用：{exc}") from exc

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        import httpx

        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            session = self._init(client, headers)
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

    def _init(self, client, headers) -> str:
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
            payload = _decode(resp)
            if "error" in payload:
                raise ValueError(str(payload["error"]))
            session = resp.headers.get("mcp-session-id")
            if not session:
                raise ValueError("initialize 未返回 Mcp-Session-Id")
            client.post(
                self.url,
                headers={**headers, "Mcp-Session-Id": session},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            return session
        raise ValueError("所有协议版本都被拒绝")


def client_from_env() -> InfoClient | None:
    url = os.environ.get("FD_INFO_URL")
    if not url:
        return None
    return HttpInfoClient(url, token=os.environ.get("FD_INFO_TOKEN"))


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
        inner = structured.get("result")
        return inner if isinstance(inner, dict) else structured
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                return json.loads(block.get("text") or "")
            except json.JSONDecodeError:
                continue
    return {}
