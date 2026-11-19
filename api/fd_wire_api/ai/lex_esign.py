"""识律电子签机器通道客户端（add-esignature-wire-actions 任务 2.1）。

wire 动作闸四动作的**唯一出网口**（fd-lex /api/admin/esignature 管理面，
``X-Admin-Token: INTERNAL_API_TOKEN`` 内部令牌，fail-closed 先例）：

- ``POST {base}/api/admin/esignature/tasks``——原子建档+发起（闸门预检在
  fd-lex 侧，阻断=422 且零残留；notice 级须显式 acknowledged，该确认由
  柏讯确认面的人类给出，机器不代决）；
- ``GET  {base}/api/admin/esignature/tasks/{task_id}?customer_id=``——查状态
  （读工具 ``esignature_status`` 数据面；他租户与不存在一律 404「任务不存在」）；
- ``POST {base}/api/admin/esignature/tasks/{task_id}/nudge``——催签（台账事件
  留痕，不外发链接）；
- ``POST {base}/api/admin/esignature/tasks/{task_id}/archive``——归档（幂等
  确保出证包，返回证据摘要）。

红线纪律（spec `wire-esignature-actions`「不代签机器红线」）：
- 本模块**只有**上述四操作，不存在任何指向签署人面（/p/、/s/、realname、
  intent、sign）的函数或路径——落签只在签署人 H5 面（链接+短会话 token）；
- **令牌绝不入异常文案/日志/repr**（wanxing 同款纪律）——错误只带状态码与
  对端 detail；
- 客户端不共享实例（per-request 建、用完 close，对齐 HttpScopeClient 惯例）；
  ``http_client`` 可注入（测试 MockTransport，注入即不持有）；
- 通道未配置（env 缺）→ :class:`LexEsignChannelNotConfigured`，调用侧如实
  报「签署通道未配置」，绝不假成功或静默排队。
"""

from __future__ import annotations

import os
from typing import Any, Protocol

import httpx

LEX_ESIGN_BASE_ENV = "LEX_ESIGN_BASE_URL"
LEX_ESIGN_TOKEN_ENV = "LEX_ESIGN_INTERNAL_TOKEN"
LEX_ESIGN_TIMEOUT_ENV = "LEX_ESIGN_TIMEOUT"

#: 超时缺省（秒）：四操作均为轻量 JSON 请求（file_b64 上行单独放宽由调用侧传）。
LEX_ESIGN_TIMEOUT_DEFAULT = 30.0

#: 机器面路由全集（与 fd-lex admin_api/esignature_routes.py 冻结契约对拍；
#: 模块级断言测试锁「不含签署人面路径」——见 tests/test_esignature_actions.py）。
MACHINE_PATHS: frozenset[str] = frozenset(
    {
        "/api/admin/esignature/tasks",
        "/api/admin/esignature/tasks/{task_id}",
        "/api/admin/esignature/tasks/{task_id}/nudge",
        "/api/admin/esignature/tasks/{task_id}/archive",
    }
)


class LexEsignChannelNotConfigured(Exception):
    """通道未配置（LEX_ESIGN_BASE_URL / LEX_ESIGN_INTERNAL_TOKEN 缺失）。"""


class LexEsignError(Exception):
    """机器通道调用失败——只带状态码与对端 detail，绝不含令牌。"""

    def __init__(
        self, message: str, *, status_code: int | None = None, reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class LexEsignClient(Protocol):
    """可注入的电子签客户端协议（HTTP 门面与测试假件都满足）。"""

    def initiate_task(self, payload: dict[str, Any]) -> dict: ...

    def get_status(self, *, customer_id: str, task_id: str) -> dict: ...

    def nudge(self, *, customer_id: str, task_id: str) -> dict: ...

    def archive_task(self, *, customer_id: str, task_id: str) -> dict: ...


class HttpLexEsignClient:
    """fd-lex 电子签机器通道的 HTTP 门面（不共享实例；用完 close 或 with）。"""

    def __init__(
        self,
        base_url: str | None = None,
        internal_token: str | None = None,
        *,
        http_client: httpx.Client | None = None,
        timeout: float | None = None,
    ) -> None:
        self._base = (
            base_url
            if base_url is not None
            else os.environ.get(LEX_ESIGN_BASE_ENV, "")
        ).rstrip("/")
        self._token = (
            internal_token
            if internal_token is not None
            else os.environ.get(LEX_ESIGN_TOKEN_ENV, "")
        ).strip()
        if not self._base or not self._token:
            raise LexEsignChannelNotConfigured(
                "签署通道未配置：LEX_ESIGN_BASE_URL / LEX_ESIGN_INTERNAL_TOKEN 缺失"
            )
        if timeout is None:
            raw = os.environ.get(LEX_ESIGN_TIMEOUT_ENV, "").strip()
            try:
                timeout = float(raw) if raw else LEX_ESIGN_TIMEOUT_DEFAULT
            except ValueError:
                timeout = LEX_ESIGN_TIMEOUT_DEFAULT
        self._owns_http = http_client is None
        # 注入的测试 client 自带 MockTransport 与自己的 timeout，不在此覆盖。
        self._http = http_client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "HttpLexEsignClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 内部 ---------------------------------------------------------------

    def _request(self, method: str, path: str, *, params=None, json=None) -> dict:
        try:
            resp = self._http.request(
                method,
                f"{self._base}{path}",
                params=params,
                json=json,
                headers={"X-Admin-Token": self._token},
            )
        except httpx.HTTPError as exc:
            raise LexEsignError(
                f"签署通道不可达：{type(exc).__name__}", reason="unreachable"
            ) from exc
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = str(body.get("detail", "")) if isinstance(body, dict) else ""
            except ValueError:
                pass
            reason = "not_found" if resp.status_code == 404 else "rejected"
            raise LexEsignError(
                detail or f"签署通道返回 {resp.status_code}",
                status_code=resp.status_code,
                reason=reason,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise LexEsignError("签署通道回执非 JSON", reason="protocol") from exc
        return data if isinstance(data, dict) else {"data": data}

    # -- 四操作（冻结契约，勿增） -------------------------------------------

    def initiate_task(self, payload: dict[str, Any]) -> dict:
        """原子建档+发起；返回任务视图（**不含签署链接**，对端红线）。"""
        return self._request("POST", "/api/admin/esignature/tasks", json=payload)

    def get_status(self, *, customer_id: str, task_id: str) -> dict:
        return self._request(
            "GET",
            f"/api/admin/esignature/tasks/{task_id}",
            params={"customer_id": customer_id},
        )

    def nudge(self, *, customer_id: str, task_id: str) -> dict:
        return self._request(
            "POST",
            f"/api/admin/esignature/tasks/{task_id}/nudge",
            json={"customer_id": customer_id},
        )

    def archive_task(self, *, customer_id: str, task_id: str) -> dict:
        return self._request(
            "POST",
            f"/api/admin/esignature/tasks/{task_id}/archive",
            json={"customer_id": customer_id},
        )


#: 任务状态（fd-lex esignature models 常量同名字符串）
STATE_PENDING = "pending"
STATE_IN_PROGRESS = "in_progress"
STATE_COMPLETED = "completed"

NUDGEABLE_STATES = frozenset({STATE_PENDING, STATE_IN_PROGRESS})
ARCHIVABLE_STATES = frozenset({STATE_COMPLETED})
