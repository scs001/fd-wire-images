"""知验台本体拉取内部端点（只读投影 + 手动拉取触发）。

鉴权沿 demands 先例：``dependencies=[Depends(_require_internal)]``
（``X-Internal-Token``，与 ``FD_WIRE_INTERNAL_TOKEN`` 常量时间比对）——
快照是平台级数据（不挂客户租户），仅运营面可读（design D2）。

路由（全部内部）：

- ``GET  /api/v1/internal/bench-ontology/domains``                    有快照域清单（最新版本/digest）
- ``GET  /api/v1/internal/bench-ontology/{domain_key}/versions``      版本时间线
- ``GET  /api/v1/internal/bench-ontology/{domain_key}/entities``      实体条目（?version=&kind=）
- ``GET  /api/v1/internal/bench-ontology/{domain_key}/relationships`` 关系条目（?version=；含重建身份）
- ``POST /api/v1/internal/bench-ontology/{domain_key}/pull``          手动拉取（body 可选 {"version": N}）

拉取失败映射（HTTP 如实，design D3）：``PullNotFoundError`` → 404（对端
语义透传）；``PullAuthError``/``PullTransportError`` → 502（对端语义保留在
detail）；``PullConfigError`` → 503（detail 指明缺哪个 env）。任何失败都已
在 ``store.pull_and_store`` 内记 ``ok=False`` 拉取日志（脱敏）——本层只做
状态映射，不再吞异常。

投影面的 ``ValueError``（快照条目缺核心键，D6 严格半边）→ 500 如实暴露
（这是**快照数据**坏了，不是请求错——不伪装成 4xx）。
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import projection
from .client import (
    PullAuthError,
    PullConfigError,
    PullNotFoundError,
    PullTransportError,
)
from .store import get_db, pull_and_store

#: 登录态读面的身份依赖（与 datasets/workbench 同源；不在内部面用）。
from ..console.auth import require_console_customer_id



def _require_internal(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """内部调用鉴权：与 demands 同款语义（FD_WIRE_INTERNAL_TOKEN 常量比对）。

    本地自持而不 ``from ..demands.routes import``：demands.routes 初始化链
    较重，bench_pull 在 app.py 的导入顺序先于 demands 时会构成循环导入。
    """
    # 单一真源在 internal_auth：未配置环境变量时它返回 503，而不是用缺省口令。
    from ..internal_auth import require_internal as _impl
    _impl(x_internal_token)

router = APIRouter(
    prefix="/api/v1/internal/bench-ontology",
    tags=["bench-ontology"],
    dependencies=[Depends(_require_internal)],
)

#: 登录态只读读面（add-bench-wire-seam-contract 任务 2.2）：与上面的内部面
#: **分属两套信任边界**，故是另一个 router 而不是同一 router 加一个「登录也
#: 可以」的分支。内部面守 ``X-Internal-Token``，此面守 console 登录；合并
#: 两者会把「平台级数据、仅运营面可读」的设计悄悄放宽。
#:
#: 只出元数据（版本/digest/条目计数/拉取时间），不返回快照正文——正文是大
#: 块 JSONB 且等于把共享层内容复制进浏览器，前端展示它没有意义（design D3）。
console_router = APIRouter(
    prefix="/api/v1/console/bench-ontology",
    tags=["bench-ontology-console"],
)


class PullIn(BaseModel):
    """手动拉取入参（body 可省）：version 缺省=拉最新发布。"""

    version: int | None = Field(default=None, description="钉历史版本 N；缺省最新")


@router.get("/domains")
def list_domains(db: Session = Depends(get_db)) -> list[dict]:
    """有快照的域清单（domain_key + 最新 version/digest；空库 → []）。"""
    return projection.list_domains_with_snapshots(db)


@router.get("/{domain_key}/versions")
def list_domain_versions(
    domain_key: str, db: Session = Depends(get_db)
) -> list[dict]:
    """某域版本时间线（version 倒序；从未拉取 → []，空域如实）。"""
    return projection.list_versions(db, domain_key=domain_key)


@router.get("/{domain_key}/entities")
def list_domain_entities(
    domain_key: str,
    db: Session = Depends(get_db),
    version: int | None = Query(default=None),
    kind: str | None = Query(default=None),
) -> list[dict]:
    """实体条目（version 缺省最新；kind 过滤；未知域 → []）。"""
    try:
        return projection.list_entities(db, domain_key=domain_key, version=version, kind=kind)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@console_router.get("")
def console_overview(
    _customer: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """标准本体快照概览（登录态只读）：每域一条元数据，无快照 → 空集。

    快照是平台级数据（不挂客户租户），所以这里**不做客户域过滤**——但也因此
    只出元数据。鉴权只要「已登录的 console 客户」，与 datasets/workbench
    同一条身份依赖。无快照时返回空列表而不是 404：那是「还没拉过」，不是
    「不存在」，前端据此渲染空态。
    """
    return {"domains": projection.snapshot_overview(db)}


@router.get("/{domain_key}/relationships")
def list_domain_relationships(
    domain_key: str,
    db: Session = Depends(get_db),
    version: int | None = Query(default=None),
) -> list[dict]:
    """关系条目（每条含重建身份 from_key/kind/to_key/external_key）。"""
    try:
        return projection.list_relationships(db, domain_key=domain_key, version=version)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{domain_key}/pull")
def pull_domain(
    domain_key: str,
    body: PullIn | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """手动拉取一域（缺省最新版）：拉取 → 幂等落库 → 记日志，返回快照行。

    失败已如实入拉取日志（store 层收口）；本层只做异常 → HTTP 状态映射。
    """
    requested_version = body.version if body is not None else None
    try:
        snapshot = pull_and_store(
            db, domain_key=domain_key, version=requested_version
        )
    except PullNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PullConfigError as exc:
        # 凭据 env 未配置——服务端配置缺失（503），detail 指明缺哪个 env。
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (PullAuthError, PullTransportError) as exc:
        # 对端拒绝（凭据/越域语义保留在 detail）或网络/形状故障——如实 502。
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return snapshot


__all__ = ["PullIn", "console_router", "router"]
