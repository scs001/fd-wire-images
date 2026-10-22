"""OpenBAO KV v2 客户端（stdlib urllib，零第三方依赖）。

生产实例 = cheap-1 ``mcp-openbao``（registry 栈成员，file backend）。cell 库
属主凭据落 ``secret/data/<path_prefix>/<customer_id>``，专用 token 的 policy
``wire-cell-db`` 仅允许该前缀（越路径拒）。

纪律：
- 三件事：``put`` / ``get`` / ``delete``，path 为逻辑路径（如
  ``wire/cell-db/acme``），内部拼 ``secret/data/`` 前缀（KV v2 API 形态）。
- 失败即 ``OpenbaoError``（消息带下一步指引），**不重试不降级**：封印态
  （503）是密封特性，postgres 画像 provision 会 fail-closed 报「先解封」
  ——这是设计，不是故障（见 docs/cell-postgres-storage.md §7.2）。
- 错误消息绝不包含 token 与请求体（凭据不出现在日志/审计/对话）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from cell_manager.errors import CellManagerError

#: KV v2 数据端点前缀（逻辑路径 → REST 路径）
_KV_V2_PREFIX = "secret/data"


class OpenbaoError(CellManagerError):
    """OpenBAO 交互失败（消息自带下一步指引；不含 token / 凭据明文）。

    继承 ``CellManagerError`` 使 Manager._run 的失败审计与 CLI 的基类捕获
    对存储故障一视同仁（形态上仍是 Exception 子类，契约不变）。
    """


def _guidance_for_status(status: int, method: str, path: str) -> str:
    """按 HTTP 状态给下一步指引（封印 / 权限 / 其余通用）。"""
    if status == 503:
        return (
            f"OpenBAO {method} secret/data/{path} failed (HTTP 503, sealed or down) — "
            "先解封 OpenBAO（mcp-openbao 重启后保持封印，unseal key 见 "
            "/root/openbao-init-*.json；封印期间 postgres 画像 provision "
            "fail-closed，这是设计，不是故障）"
        )
    if status == 403:
        return (
            f"OpenBAO {method} secret/data/{path} failed (HTTP 403) — token 权限不足："
            "核对 CELL_OPENBAO_TOKEN 与 policy wire-cell-db（仅允许 "
            "secret/data/wire/cell-db/* 与 metadata）"
        )
    if status == 404:
        return f"OpenBAO {method} secret/data/{path} failed (HTTP 404) — 路径不存在"
    return f"OpenBAO {method} secret/data/{path} failed (HTTP {status})"


class OpenbaoClient:
    """极薄 KV v2 客户端：put/get/delete，失败带指引，不重试不降级。"""

    def __init__(self, addr: str, token: str, timeout: float = 10) -> None:
        self._addr = (addr or "").rstrip("/")
        self._token = token
        self._timeout = timeout

    @property
    def addr(self) -> str:
        return self._addr

    # ---- 对外三件套 ----

    def put(self, path: str, data: dict[str, Any]) -> None:
        """写入条目（KV v2：body 为 ``{"data": {...}}``）。非 2xx 即抛错。"""
        self._request("PUT", path, body={"data": dict(data)})

    def get(self, path: str) -> dict[str, Any] | None:
        """读条目；404 → None（幂等探测语义），其余非 2xx 抛错。"""
        status, payload = self._request("GET", path)
        if payload is None:
            return None
        try:
            inner = payload["data"]["data"]  # KV v2 双层嵌套
        except (KeyError, TypeError) as exc:
            raise OpenbaoError(
                f"OpenBAO GET secret/data/{path} returned unexpected payload shape "
                f"(missing data.data): {type(exc).__name__}"
            ) from exc
        return inner

    def delete(self, path: str) -> None:
        """删除条目（幂等：404 视为成功）。"""
        self._request("DELETE", path)

    # ---- 内部 ----

    def _url(self, path: str) -> str:
        return f"{self._addr}/v1/{_KV_V2_PREFIX}/{path.lstrip('/')}"

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        """发请求；返回 (status, 解析后的 JSON 或 None)。404 与失败统一走这里。"""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            request = urllib.request.Request(
                self._url(path),
                data=data,
                method=method,
                headers={
                    "X-Vault-Token": self._token,
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and method in ("GET", "DELETE"):
                return 404, None  # get → None；delete → 幂等成功
            raise OpenbaoError(_guidance_for_status(exc.code, method, path)) from exc
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            # ValueError：无 scheme 的地址（urllib 对 unknown url type 直接抛）
            raise OpenbaoError(
                f"连接 OpenBAO 失败 ({self._addr}, {type(exc).__name__}: {exc}) — "
                "地址核对：检查 CELL_OPENBAO_ADDR 与网络可达性"
            ) from exc
        payload: Any = None
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise OpenbaoError(
                    f"OpenBAO {method} secret/data/{path} returned malformed JSON: {exc}"
                ) from exc
        return status, payload
