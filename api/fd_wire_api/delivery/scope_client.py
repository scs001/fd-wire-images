"""fd-open-data-mcp 指标 scope API 客户端（wire-platform-v1 任务 6.6）。

交付默认进客户私有 scope——scope 的创建/授权/列举经本客户端走显式
HTTP 契约（README「借用契约」纪律），端点形状对齐 fd-open-data-mcp 的
scoping 服务语义（只读参考，不改那个仓）：

- ``POST  {base}/scopes``          ← ``scoping.scope_create(name, rules,
  description)``（fd_open_data_mcp/scoping.py:155；MCP 工具 ``scope_create``
  见 fd_open_data_mcp/server.py:1082）。body ``{"name", "description",
  "rules": {source_dbs, domains, semantic_codes, native_codes}}``——rules 是
  四维 allow-list，空列表维度即"该维不约束"；至少一维非空，否则空 scope
  被拒（scoping.normalize_rules，scoping.py:60）。
- ``GET   {base}/scopes``          ← ``scoping.scope_list``（scoping.py:224；
  server.py:1106）。返回 ``[{name, description, rules, ...}]``。
- ``PATCH {base}/scopes/{name}``   ← ``scoping.scope_update(name, rules)``
  （scoping.py:184；server.py:1118）。``grant_indicator`` 以「读取→合并
  semantic_codes→回写」实现，复用真实的 list+update 两个操作形状。
- ``POST  {base}/indicators/publish`` / ``.../unpublish`` ← registry 的
  ``verified`` 审核旗语义（fd_open_data_mcp/models.py:101——verified 即入
  公开可读面）。该治理面随任务 7.2 目录治理台落地，此处先固定调用形状。

鉴权：``Authorization: Bearer <SCOPE_API_TOKEN>``（env 可重指）。错误以
``{"error": "invalid_scope"|"unknown_scope", "detail": ...}`` 载荷返回
（server.py:1072 ``_scope_error``）——本客户端统一转为
:class:`ScopeClientError`，绝不把服务端错误静默吞掉。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from typing import Any, Protocol

import httpx

SCOPE_API_BASE_ENV = "SCOPE_API_BASE"
SCOPE_API_TOKEN_ENV = "SCOPE_API_TOKEN"

#: rules 的四个 allow-list 维度（对齐 scoping.RULE_DIMENSIONS）。
RULE_DIMENSIONS: tuple[str, ...] = (
    "source_dbs",
    "domains",
    "semantic_codes",
    "native_codes",
)


class ScopeClientError(Exception):
    """scope API 不可达、返回非 2xx 或载荷不合契约。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ScopeClient(Protocol):
    """可注入的 scope 客户端协议（HttpScopeClient 与测试 mock 都满足）。"""

    def create_scope(
        self,
        name: str,
        summary: str,
        source_dbs: Sequence[str] = (),
        *,
        semantic_codes: Sequence[str] = (),
    ) -> dict:
        """建一个命名 scope（summary 落 description；allow-list 见上）。"""
        ...

    def grant_indicator(self, *, scope_name: str, indicator_code: str) -> dict:
        """把一个指标 code 追加进 scope 的 semantic_codes allow-list。"""
        ...

    def list_scopes(self) -> list[dict]:
        """列出全部 scope（含 rules）。"""
        ...

    def publish_indicators(self, indicator_codes: Sequence[str]) -> dict:
        """指标转公共（registry verified 审核旗语义）。"""
        ...

    def unpublish_indicators(self, indicator_codes: Sequence[str]) -> dict:
        """撤回公开授权，回归客户私有。"""
        ...


def build_rules(
    source_dbs: Sequence[str], semantic_codes: Sequence[str]
) -> dict[str, list[str]]:
    """组装 rules 载荷：去重排序；全空即拒（空 scope 会被真实 API 拒）。"""
    rules = {
        "source_dbs": sorted(dict.fromkeys(s.strip() for s in source_dbs if s.strip())),
        "domains": [],
        "semantic_codes": sorted(
            dict.fromkeys(c.strip() for c in semantic_codes if c.strip())
        ),
        "native_codes": [],
    }
    if not any(rules[dim] for dim in RULE_DIMENSIONS):
        raise ScopeClientError(
            "scope 规则不含任何 allow-list（source_dbs/semantic_codes 全空）"
            "——空 scope 会被真实 API 拒绝（scoping.normalize_rules）"
        )
    return rules


class HttpScopeClient:
    """fd-open-data-mcp scope API 的 HTTP 门面（不共享实例；用完 close 或 with）。"""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        http_client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base = (
            base_url if base_url is not None else os.environ.get(SCOPE_API_BASE_ENV, "")
        ).rstrip("/")
        self._token = (
            token if token is not None else os.environ.get(SCOPE_API_TOKEN_ENV, "")
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)

    # -- 基础设施 ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "HttpScopeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _request(
        self, method: str, path: str, *, json_body: dict | None = None
    ) -> Any:
        if not self._base:
            raise ScopeClientError(
                f"scope API 未配置（{SCOPE_API_BASE_ENV} 为空），无法调用 {method} {path}"
            )
        url = f"{self._base}{path}"
        try:
            resp = self._http.request(
                method, url, json=json_body, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise ScopeClientError(f"scope API 不可达（{url}）：{exc}") from exc
        if resp.status_code >= 400:
            raise ScopeClientError(
                f"scope API {method} {path} → {resp.status_code}：{resp.text[:200]}",
                status_code=resp.status_code,
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise ScopeClientError(
                f"scope API {method} {path} 响应不是 JSON：{resp.text[:200]}"
            ) from exc
        if isinstance(body, dict) and body.get("error"):
            # 服务端显式错误载荷（_scope_error 形状）——如实上抛，绝不吞掉。
            raise ScopeClientError(
                f"scope API 拒绝 {method} {path}："
                f"{body.get('error')}: {body.get('detail')}"
            )
        return body

    # -- 协议实现 ---------------------------------------------------------

    def create_scope(
        self,
        name: str,
        summary: str,
        source_dbs: Sequence[str] = (),
        *,
        semantic_codes: Sequence[str] = (),
    ) -> dict:
        rules = build_rules(source_dbs, semantic_codes)
        return self._request(
            "POST",
            "/scopes",
            json_body={"name": name, "description": summary, "rules": rules},
        )

    def grant_indicator(self, *, scope_name: str, indicator_code: str) -> dict:
        """读改合并 semantic_codes（list + update 两个真实操作形状）。"""
        scopes = self.list_scopes()
        target = next((s for s in scopes if s.get("name") == scope_name), None)
        if target is None:
            raise ScopeClientError(f"scope {scope_name!r} 不存在，无法授权指标")
        rules = dict(target.get("rules") or {})
        codes = list(rules.get("semantic_codes") or [])
        if indicator_code in codes:
            return target  # 幂等：已在 allow-list
        codes.append(indicator_code)
        rules["semantic_codes"] = sorted(dict.fromkeys(codes))
        return self._request(
            "PATCH", f"/scopes/{scope_name}", json_body={"rules": rules}
        )

    def list_scopes(self) -> list[dict]:
        body = self._request("GET", "/scopes")
        if not isinstance(body, list):
            raise ScopeClientError(f"scope API GET /scopes 应返回列表，得到：{type(body).__name__}")
        return body

    def publish_indicators(self, indicator_codes: Sequence[str]) -> dict:
        return self._request(
            "POST",
            "/indicators/publish",
            json_body={"indicator_codes": list(indicator_codes)},
        )

    def unpublish_indicators(self, indicator_codes: Sequence[str]) -> dict:
        return self._request(
            "POST",
            "/indicators/unpublish",
            json_body={"indicator_codes": list(indicator_codes)},
        )


# ---------------------------------------------------------------- 依赖出口

_SCOPE_CLIENT_FACTORY: Callable[[], ScopeClient | None] | None = None


def set_scope_client_factory(factory: Callable[[], ScopeClient | None] | None) -> None:
    """注入客户端工厂（测试用；传 None 恢复默认出口）。"""
    global _SCOPE_CLIENT_FACTORY
    _SCOPE_CLIENT_FACTORY = factory


def reset_scope_client_factory() -> None:
    """清除注入，恢复默认出口（env 驱动）。"""
    global _SCOPE_CLIENT_FACTORY
    _SCOPE_CLIENT_FACTORY = None


def get_scope_client() -> ScopeClient | None:
    """解析当前 scope 客户端：测试注入的工厂 > env ``SCOPE_API_BASE``。

    未配置时返回 None——交付流程据此如实把私有 scope 交付标 ``pending``
    （失败留档，绝不假装交付成功）。
    """
    if _SCOPE_CLIENT_FACTORY is not None:
        return _SCOPE_CLIENT_FACTORY()
    base = os.environ.get(SCOPE_API_BASE_ENV, "").strip()
    if not base:
        return None
    return HttpScopeClient(base_url=base)


def scope_client_dep() -> Iterator[ScopeClient | None]:
    """FastAPI 依赖：每请求一个客户端，自建的自负责关闭。"""
    client = get_scope_client()
    try:
        yield client
    finally:
        if isinstance(client, HttpScopeClient):
            client.close()
