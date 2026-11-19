"""匿名橱窗 API 路由（wire-showcase-market）。

- ``GET /api/v1/showcase/dashboards?limit=&offset=``（**匿名，零鉴权依赖**）：
  平台自营发布方已上架、market 档的看板列表。
- ``GET /api/v1/showcase/dashboards/{artifact_id}``（匿名）：钉当前版本的完整
  spec（含面板快照）——匿名橱窗要画得出图。

红线（design D2/D3）：
- 本模块**不引入任何身份依赖**（无 ``Depends(require_console_customer_id)``
  之类）——客户的产物在物理上不在这条路径的查询范围里（见 ``showcase.py``
  的三条硬条件）。绝不把匿名性做成既有市场端点的「可选鉴权」，那样一个漏写
  依赖就是一次静默泄露。
- 详情对任何非平台自营产物一律 **404（不是 403）**——403 会泄露「此处存在
  一个客户产物」。

工程骨架照分享端点（wire-dashboard-share）：独立 env 阈值的进程内滑动窗限流
（按客户端 IP 作键，匿名面没有令牌）+ 60 秒短缓存（缓存键含列表参数）+ 统一
404 文案。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session
from fastapi import Depends

from ..admin.db import get_db as get_platform_db
from ..ai.db import get_db
from ..share.rate_limit import SlidingWindowLimiter
from . import showcase

router = APIRouter(prefix="/api/v1/showcase", tags=["showcase"])

#: 橱窗响应的缓存有效期（秒）——供 CDN 与官网静态构建复用。
SHOWCASE_CACHE_SECONDS = 60

#: 匿名面限流缺省（每 IP 每小时）。读面阈值宽于写入面，但必须有上限：
#: 没有限流就是一个匿名可打的数据库查询入口。
DEFAULT_SHOWCASE_RATE_PER_HOUR = 600


def showcase_rate_per_hour() -> int:
    """阈值：逐请求惰性读 ``FD_WIRE_SHOWCASE_RATE_PER_HOUR``（缺省 600；
    空白/非整数/非正数一律回退缺省——fail-closed 到保守值）。"""
    import os

    raw = os.getenv("FD_WIRE_SHOWCASE_RATE_PER_HOUR", "").strip()
    if not raw:
        return DEFAULT_SHOWCASE_RATE_PER_HOUR
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SHOWCASE_RATE_PER_HOUR
    return value if value > 0 else DEFAULT_SHOWCASE_RATE_PER_HOUR


#: 进程内限流器：键 = 客户端标识（IP）。与分享端点分机器（独立 env 阈值）。
_showcase_limiter = SlidingWindowLimiter(rate_fn=showcase_rate_per_hour)

# ── 60 秒短缓存（公开面反复刷新不放大查询）─────────────────────────────
_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX_ENTRIES = 256
_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached(key: tuple, build: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
    payload = build()
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            for stale, entry in list(_cache.items()):
                if now - entry[0] >= _CACHE_TTL_SECONDS:
                    _cache.pop(stale, None)
        _cache[key] = (now, payload)
    return payload


def reset_showcase_cache() -> None:
    """清空缓存与限流窗位（测试复位入口）。"""
    with _cache_lock:
        _cache.clear()
    _showcase_limiter.clear()


def _client_key(request: Request) -> str:
    """限流键：优先取反代写入的真实客户端 IP，回退套接字地址。

    匿名面没有令牌可作键，IP 是唯一可用维度；同一出口 NAT 后的多个访客共享
    一个桶——这是 v0 明示边界（阈值取得足够宽，只拦脚本级滥用）。
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = request.client
    return client.host if client is not None else "unknown"


def _rate_gate(request: Request) -> None:
    allowed, retry_after = _showcase_limiter.allow(_client_key(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后再试",
            headers={"Retry-After": str(retry_after)},
        )


def _set_cache_headers(response: Response) -> None:
    """缓存提示：公开只读内容的公共缓存窗口（CDN/静态构建可安全复用）。"""
    response.headers["Cache-Control"] = f"public, max-age={SHOWCASE_CACHE_SECONDS}"


@router.get("/dashboards")
def list_showcase_dashboards(
    request: Request,
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    platform_db: Session = Depends(get_platform_db),
) -> dict:
    """匿名橱窗列表：平台自有、已上架、market 档看板。

    平台租户缺失时返回空列表（安全默认——宁可橱窗空，不让服务 500，更不
    因缺失放宽任何过滤）。
    """
    _rate_gate(request)
    payload = _cached(
        ("list", limit, offset),
        lambda: showcase.list_showcase_dashboards(
            db, platform_db, limit=limit, offset=offset
        ),
    )
    _set_cache_headers(response)
    return payload


@router.get("/dashboards/{artifact_id}")
def get_showcase_dashboard(
    artifact_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    platform_db: Session = Depends(get_platform_db),
) -> dict:
    """匿名橱窗详情：钉当前版本，返回完整 spec（含快照）。

    任何非平台自营产物一律 404（统一文案，不泄露差异与存在性）。
    """
    _rate_gate(request)
    payload = _cached(
        ("detail", artifact_id),
        lambda: showcase.get_showcase_dashboard(db, platform_db, artifact_id),
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="not found")
    _set_cache_headers(response)
    return payload


__all__ = [
    "SHOWCASE_CACHE_SECONDS",
    "reset_showcase_cache",
    "router",
    "showcase_rate_per_hour",
]
