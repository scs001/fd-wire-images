"""知验台分发面（law-bench C3）拉取客户端。

对端契约：``GET {BENCH_PULL_URL}/api/v1/distribution/{domain_key}/ontology``，
头 ``X-Consumer-Token``（可选 ``?version=N`` 钉历史版本，缺省最新）。载荷
8 顶层键（插入序）::

    domain / template_name / template_version / publication_version /
    digest / entities / relationships / changelog

键形状以 law-bench ``src/web/routes/distribution.py`` 模块 docstring 为准
（来源 sha-43f7063）——本仓刻意**不 import 对端任何东西**（松耦合铁律，
design D1）；形状漂移由契约测试兜底（D5）。

三态错误语义（如实透传，消息保留对端 detail 文本）：

===========  ==========================================================
401          缺少机器凭据：X-Consumer-Token 请求头
403          令牌未登记 / 令牌已吊销 / 该域不在绑定范围（三态可区分）
404          该域尚无发布 / 该域无版本 v{N}
===========  ==========================================================

凭据纪律（spec 红线）：token 只经 env（``BENCH_CONSUMER_TOKEN``）进入
进程，函数入参可显式覆盖（测试用）；任何异常消息/日志**永不包含 token**——
构造消息后统一过一遍脱敏（命中即整词替换为 ``***``）。

HTTP 客户端刻意 ``trust_env=False``（xingyan/client.py 同款先例）：集群内
系统代理劫持回环请求有先例（502 假故障）。
"""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx

#: 对端拉取基址 env（如 ``https://bench.finddatatech.cloud``，无尾斜杠语义
#: 由本模块 rstrip("/") 规整）。
URL_ENV = "BENCH_PULL_URL"
#: 消费者令牌 env（值只进请求头，永不落日志/异常/数据库）。
TOKEN_ENV = "BENCH_CONSUMER_TOKEN"

#: 拉取缺省超时（秒）。
DEFAULT_TIMEOUT_SECONDS = 10.0

#: 载荷 8 顶层键（对端 C3 契约；缺失即形状漂移——PullTransportError）。
PAYLOAD_TOP_KEYS: tuple[str, ...] = (
    "domain",
    "template_name",
    "template_version",
    "publication_version",
    "digest",
    "entities",
    "relationships",
    "changelog",
)


# ---------------------------------------------------------------- 异常四态


class PullConfigError(RuntimeError):
    """凭据/基址 env 缺失——消息指明缺哪个 env（惰性，不使应用崩溃）。"""


class PullAuthError(RuntimeError):
    """对端 401/403——缺凭据/令牌未登记/令牌已吊销/该域不在绑定范围。"""


class PullNotFoundError(RuntimeError):
    """对端 404——该域尚无发布 / 该域无版本 vN（如实透传语义）。"""


class PullTransportError(RuntimeError):
    """网络/超时/非 JSON/载荷形状漂移/5xx——绝不误标成功。"""


# ---------------------------------------------------------------- 内部工具


#: 脱敏最小令牌长度：真令牌（token_urlsafe(32)≈43 字符）远长于此。退化短串
#: （如测试占位单字符）不做子串替换——'t' 会命中 'timeout' 把消息打成筛子。
_MIN_REDACT_LEN = 8


def _redact(message: str, token: str | None) -> str:
    """token 若出现在消息里则替换为 ``***``（纵深防御）。

    token 本就绝不进消息（只走请求头；httpx 异常文本不含请求头），替换只防
    「对端把 token 回显进错误 detail」这一条路径；只对长度达标（≥8）的真
    令牌生效，短占位串不误伤正常消息。
    """
    if token and len(token) >= _MIN_REDACT_LEN and token in message:
        return message.replace(token, "***")
    return message


def _error_detail(resp: httpx.Response) -> str:
    """从对端错误响应取 detail 文本（FastAPI 形态 ``{"detail": ...}`` 优先，
    其余如实回退状态码；响应体永不回显 token——对端本就不回显，本端兜底）。"""
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"]
    return f"HTTP {resp.status_code}"


# ---------------------------------------------------------------- 客户端主体


def pull_ontology(
    *,
    domain_key: str,
    version: int | None = None,
    base_url: str | None = None,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """拉一个域的本体发布快照（200 → 8 键 dict；其余按三态映射抛专用异常）。

    env 惰性读取（函数内）：``base_url``/``token`` 入参缺省时回落
    ``BENCH_PULL_URL``/``BENCH_CONSUMER_TOKEN``——缺失即抛
    :class:`PullConfigError` 并**指明缺哪个 env**（零配置部署零崩溃，
    design D3）。``transport`` 仅供契约测试注入 httpx MockTransport
    （D5），生产恒为 None。

    异常映射：401/403 → :class:`PullAuthError`；404 →
    :class:`PullNotFoundError`（对端 detail 文本原样保留）；网络/超时/
    非 JSON/形状漂移/其它状态码 → :class:`PullTransportError`。所有消息
    脱敏（token 永不出现在异常文本里）。
    """
    resolved_base = (base_url or os.environ.get(URL_ENV, "")).strip().rstrip("/")
    if not resolved_base:
        raise PullConfigError(f"缺少配置：环境变量 {URL_ENV} 未设置（知验台分发面基址）")
    resolved_token = (token if token is not None else os.environ.get(TOKEN_ENV, "")).strip()
    if not resolved_token:
        raise PullConfigError(f"缺少配置：环境变量 {TOKEN_ENV} 未设置（知验台消费者令牌）")

    url = f"{resolved_base}/api/v1/distribution/{quote(domain_key, safe='')}/ontology"
    params = {"version": version} if version is not None else None
    headers = {"X-Consumer-Token": resolved_token}

    try:
        with httpx.Client(
            timeout=timeout, transport=transport, trust_env=False
        ) as client:
            resp = client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        # 类型名 + 摘要；httpx 异常文本不含请求头（token 不可能泄漏）。
        raise PullTransportError(
            _redact(f"拉取不可达：{type(exc).__name__}: {exc}", resolved_token)
        ) from exc

    if resp.status_code == 200:
        return _parse_payload(resp, token=resolved_token)

    detail = _redact(_error_detail(resp), resolved_token)
    if resp.status_code == 401:
        raise PullAuthError(f"对端拒绝（缺凭据）：{detail}")
    if resp.status_code == 403:
        # 未登记 / 已吊销 / 越域三态语义原样透传（消息即对端 detail）。
        raise PullAuthError(f"对端拒绝：{detail}")
    if resp.status_code == 404:
        raise PullNotFoundError(f"对端无发布：{detail}")
    raise PullTransportError(
        _redact(f"拉取失败：HTTP {resp.status_code}: {detail}", resolved_token)
    )


def _parse_payload(resp: httpx.Response, *, token: str) -> dict:
    """200 响应体 → 8 顶层键校验后的 dict（形状漂移 = PullTransportError）。"""
    try:
        payload = resp.json()
    except ValueError as exc:
        raise PullTransportError(
            _redact("拉取失败：响应不是合法 JSON", token)
        ) from exc
    if not isinstance(payload, dict):
        raise PullTransportError(
            _redact("unexpected payload shape: 顶层不是对象", token)
        )
    missing = [key for key in PAYLOAD_TOP_KEYS if key not in payload]
    if missing:
        raise PullTransportError(
            _redact(
                f"unexpected payload shape: 缺少顶层键 {', '.join(missing)}",
                token,
            )
        )
    return payload


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "PAYLOAD_TOP_KEYS",
    "TOKEN_ENV",
    "URL_ENV",
    "PullAuthError",
    "PullConfigError",
    "PullNotFoundError",
    "PullTransportError",
    "pull_ontology",
]
