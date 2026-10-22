"""萬星 A2A 直连客户端（wire-wanxing-direct-dispatch 任务 1）。

wire 对 spider-heal agent 的**第二条工单投递通道**（快车道）：与舰队
拉取通道（零外部凭据、~30min 周期）并存，操作员显式选择。调用形状按
2026-10-12 生产实弹探针验证过的契约冻结（design D1）：

- 端点：``POST {WANXING_A2A_BASE}/api/wanxing/v1/a2a/{WANXING_AGENT_SLUG}``
  （新域 wanxing.finddatatech.cloud——**绝不调** platform 旧 facade：双
  部署两份 SQLite，白名单/幂等互不相通）；
- 头：``Authorization: Bearer <WANXING_CALLER_KEY>``（secret 注入）、
  ``Idempotency-Key``（service 层按工单内容 sha256 派生——同内容同键，
  wanxing 侧去重口径）、``Content-Type: application/json``；
- 体：JSON-RPC ``message/send``，工单 YAML 全文作为唯一 text part
  （同一脱敏映射保证红线：工单里本来就没有邮箱/公司名）。

红线纪律：
- **键值绝不入异常文案/日志/repr**——错误只带 HTTP 状态码与 wanxing
  回传的 error 码（:class:`WanxingInvokeError`）。
- 客户端不共享实例（per-request 建、用完 close，对齐 HttpScopeClient
  惯例）；``http_client`` 可注入（测试 MockTransport，注入即不持有）。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import Any, Protocol

import httpx

WANXING_A2A_BASE_ENV = "WANXING_A2A_BASE"
WANXING_AGENT_SLUG_ENV = "WANXING_AGENT_SLUG"
WANXING_CALLER_KEY_ENV = "WANXING_CALLER_KEY"
WANXING_A2A_TIMEOUT_ENV = "WANXING_A2A_TIMEOUT"

#: 新域缺省（design D1 env 表；deploy 明文注入同值，env 只做兜底）。
WANXING_A2A_BASE_DEFAULT = "https://wanxing.finddatatech.cloud"
#: spider-heal agent slug 缺省（允泣清单已加该 caller，2026-10-12 实证）。
WANXING_AGENT_SLUG_DEFAULT = "packs-kkcie7nlrhluo4likpnn0w-spider-heal"
#: 超时缺省（秒）：message/send 同步回执、flash 模型秒级常返，120s 留余量。
WANXING_TIMEOUT_DEFAULT = 120.0


class WanxingInvokeError(Exception):
    """A2A 调用失败（不可达/鉴权/协议失配/其余 4xx-5xx）——绝不带键值。

    ``reason`` 定位失败域供留档与运营判断：
    ``unauthorized``（401 调用键无效）/ ``forbidden``（403 调用者不在
    agent 允许清单）/ ``unreachable``（网络/httpx 异常）/ ``protocol``
    （非 JSON 或回执形态不合契约）/ ``None``（其余 >=400，按状态码原文）。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class WanxingClient(Protocol):
    """可注入的萬星客户端协议（HttpWanxingClient 与测试假件都满足）。"""

    def send_message(
        self, text: str, *, idempotency_key: str, rpc_id: str | None = None
    ) -> dict:
        """发一条 message/send，返回 ``{"http_status", "message_id", "reply_text"}``。"""
        ...


class HttpWanxingClient:
    """萬星 A2A 的 HTTP 门面（不共享实例；用完 close 或 with）。

    构造参数留 None 即走 env（缺省值见模块头）——生产路径经
    :func:`get_wanxing_client` 出口统一解析，测试直接注入 ``http_client``
    （MockTransport）并自持有其生命周期。
    """

    def __init__(
        self,
        base_url: str | None = None,
        caller_key: str | None = None,
        agent_slug: str | None = None,
        *,
        http_client: httpx.Client | None = None,
        timeout: float | None = None,
    ) -> None:
        self._base = (
            base_url
            if base_url is not None
            else os.environ.get(WANXING_A2A_BASE_ENV, WANXING_A2A_BASE_DEFAULT)
        ).rstrip("/")
        self._slug = (
            agent_slug
            if agent_slug is not None
            else os.environ.get(WANXING_AGENT_SLUG_ENV, WANXING_AGENT_SLUG_DEFAULT)
        )
        self._key = (
            caller_key
            if caller_key is not None
            else os.environ.get(WANXING_CALLER_KEY_ENV, "")
        ).strip()
        if timeout is None:
            raw = os.environ.get(WANXING_A2A_TIMEOUT_ENV, "").strip()
            try:
                timeout = float(raw) if raw else WANXING_TIMEOUT_DEFAULT
            except ValueError:
                # 非法配置不炸通道：退缺省并在调用侧如实留档，配置问题人工修。
                timeout = WANXING_TIMEOUT_DEFAULT
        self._owns_http = http_client is None
        # 注意：httpx.Client(timeout=...) 才是 per-client 生效位——注入的
        # 测试 client 自带 MockTransport 与自己的 timeout，不在此覆盖。
        self._http = http_client or httpx.Client(timeout=timeout)

    # -- 基础设施 ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "HttpWanxingClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 协议实现 ---------------------------------------------------------

    def send_message(
        self, text: str, *, idempotency_key: str, rpc_id: str | None = None
    ) -> dict:
        """POST message/send 并解析回执——失败抛 :class:`WanxingInvokeError`。

        - ``messageId`` 请求侧生成 uuid4（回执 ``result.messageId`` 如带回
          则优先回执——两侧都可追溯）；
        - ``reply_text`` = ``result.parts`` 里全部 text part 按序拼接；
        - ``result`` 缺席/形态不合 → 协议错（绝不把半吊子回执当成功）。
        """
        message_id = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "id": rpc_id if rpc_id is not None else str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "kind": "message",
                    "messageId": message_id,
                    "parts": [{"kind": "text", "text": text}],
                }
            },
        }
        url = f"{self._base}/api/wanxing/v1/a2a/{self._slug}"
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
        }
        try:
            resp = self._http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            # 网络面异常只留传输层事实（httpx 文案只含 URL/原因，不含头）。
            raise WanxingInvokeError(
                f"萬星 A2A 不可达（{self._base}）：{exc}", reason="unreachable"
            ) from exc
        if resp.status_code in (401, 403):
            # 鉴权面：错误定位只带状态码与 wanxing 回传的 error 码——
            # 红线：键值与请求头绝不入文案。
            code = self._error_code(resp)
            reason = "unauthorized" if resp.status_code == 401 else "forbidden"
            raise WanxingInvokeError(
                f"萬星 A2A 鉴权失败（HTTP {resp.status_code}"
                f"{'，error=' + code if code else ''}）——"
                + (
                    "调用键无效（WANXING_CALLER_KEY）"
                    if reason == "unauthorized"
                    else "调用者不在 agent 允许清单"
                ),
                status_code=resp.status_code,
                reason=reason,
            )
        if resp.status_code >= 400:
            # 其余 4xx/5xx 按状态码原文（服务端回文体，不含我方键值）。
            raise WanxingInvokeError(
                f"萬星 A2A 调用失败（HTTP {resp.status_code}）：{resp.text[:200]}",
                status_code=resp.status_code,
            )
        try:
            body: Any = resp.json()
        except ValueError as exc:
            raise WanxingInvokeError(
                f"萬星 A2A 响应不是 JSON（HTTP {resp.status_code}）：{resp.text[:200]}",
                status_code=resp.status_code,
                reason="protocol",
            ) from exc
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise WanxingInvokeError(
                "萬星 A2A 回执缺 result 或形态不合（JSON-RPC error/非对象）——"
                f"HTTP {resp.status_code}：{resp.text[:200]}",
                status_code=resp.status_code,
                reason="protocol",
            )
        parts = result.get("parts")
        if not isinstance(parts, list):
            raise WanxingInvokeError(
                "萬星 A2A 回执 result.parts 形态不合（非列表）——"
                f"HTTP {resp.status_code}",
                status_code=resp.status_code,
                reason="protocol",
            )
        texts = [
            str(p.get("text"))
            for p in parts
            if isinstance(p, dict) and p.get("kind") == "text"
        ]
        if not texts:
            raise WanxingInvokeError(
                "萬星 A2A 回执不含任何 text part（agent 无话可回=形态不合）——"
                f"HTTP {resp.status_code}",
                status_code=resp.status_code,
                reason="protocol",
            )
        echoed = result.get("messageId")
        return {
            "http_status": resp.status_code,
            "message_id": str(echoed) if echoed else message_id,
            "reply_text": "".join(texts),
        }

    @staticmethod
    def _error_code(resp: httpx.Response) -> str | None:
        """从鉴权失败回体里提 wanxing 的 error 码（提不出就 None）。"""
        try:
            body = resp.json()
        except ValueError:
            return None
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, str):
                return error
            if isinstance(error, dict) and error.get("code") is not None:
                return str(error["code"])
        return None


# ---------------------------------------------------------------- 依赖出口

_WANXING_CLIENT_FACTORY: Callable[[], WanxingClient | None] | None = None


def set_wanxing_client_factory(
    factory: Callable[[], WanxingClient | None] | None,
) -> None:
    """注入客户端工厂（测试用；传 None 等价 reset）。"""
    global _WANXING_CLIENT_FACTORY
    _WANXING_CLIENT_FACTORY = factory


def reset_wanxing_client_factory() -> None:
    """清除注入，恢复默认出口（env 驱动）。"""
    global _WANXING_CLIENT_FACTORY
    _WANXING_CLIENT_FACTORY = None


def get_wanxing_client() -> WanxingClient | None:
    """解析当前萬星客户端：测试注入的工厂 > env ``WANXING_CALLER_KEY``。

    调用键缺席（空串/空白）返回 None——service 层据此 503 如实回绝
    （未配置不是投递事实，零留档），绝不假装配置齐全。
    """
    if _WANXING_CLIENT_FACTORY is not None:
        return _WANXING_CLIENT_FACTORY()
    if not os.environ.get(WANXING_CALLER_KEY_ENV, "").strip():
        return None
    return HttpWanxingClient()
