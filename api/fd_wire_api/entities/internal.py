"""实体读面机器通道（add-entity-bench-contract 任务 3.1/3.2）。

内部可信服务（知验台 wire-connector）的**只读**图谱/列表镜像：

- 鉴权：``X-Internal-Token`` 与 ``FD_WIRE_INTERNAL_TOKEN`` 常量时间比对
  （与 demands 内部端点同源 secret）。**无 dev 缺省**——env 未设即 503
  报精确缺项（比 demands 的 dev 缺省更严：实体面是客户数据面，宁可
  拒绝服务不留已知令牌后门）。
- 客户 scope：请求**显式**携带 ``?customer_id=``（缺失 422）；白名单 env
  ``FD_WIRE_INTERNAL_ENTITY_CUSTOMERS``（逗号分隔）之外一律 404 如实
  （不泄露他企存在性）。
- 硬边界：本路由**只有 GET**——写面仅 console 身份可达（spec「机器面
  无写端点」）；响应形状与 console 面同源（service.list_entities /
  entity_graph 直出，含 tier/sensitivity 出参）。
"""

from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..datasets.db import get_db as get_datasets_db
from .db import get_db as get_entities_db
from .routes import _entity_out
from . import service

router = APIRouter(prefix="/api/v1/internal/entities", tags=["entities-internal"])

#: scope 白名单 env（逗号分隔客户键；未设=机器面禁用）
SCOPE_ENV = "FD_WIRE_INTERNAL_ENTITY_CUSTOMERS"


def _require_internal_token(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """机器令牌鉴权：同源 secret（FD_WIRE_INTERNAL_TOKEN）常量时间比对。

    缺配置 503 报精确缺项；无/坏令牌 401（与 demands 口径一致，但无 dev
    缺省——实体面不设已知令牌后门）。"""
    expected = os.getenv("FD_WIRE_INTERNAL_TOKEN")
    if expected is None or not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"机器通道缺配置：{SCOPE_ENV.replace('CUSTOMERS', 'TOKEN')}"
            "（FD_WIRE_INTERNAL_TOKEN）未设置——实体机器面禁用",
        )
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="内部端点需要有效的 X-Internal-Token",
        )


def _customer_in_scope(customer_id: str) -> None:
    """客户 scope 白名单闸：白名单外一律 404（不泄露存在性）。"""
    raw = os.getenv(SCOPE_ENV, "")
    allowed = {item.strip() for item in raw.split(",") if item.strip()}
    if customer_id not in allowed:
        raise HTTPException(status_code=404, detail="实体不存在")


@router.get("")
def internal_list_entities(
    customer_id: str = Query(min_length=1),
    _token: None = Depends(_require_internal_token),
    db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
) -> dict:
    """本企业实体列表（形状与 console ``GET /api/v1/entities`` 逐字一致，
    含 tier/sensitivity 出参；白名单外 404）。"""
    _customer_in_scope(customer_id)
    return {
        "entities": [
            _entity_out(entity, datasets_db).model_dump(mode="json")
            for entity in service.list_entities(db, customer_id=customer_id)
        ]
    }


@router.get("/graph")
def internal_get_graph(
    customer_id: str = Query(min_length=1),
    _token: None = Depends(_require_internal_token),
    db: Session = Depends(get_entities_db),
    datasets_db: Session = Depends(get_datasets_db),
) -> dict:
    """同源图谱镜像（形状与 console ``GET /api/v1/entities/graph`` 逐字一致，
    含 tier/sensitivity 出参；空图谱 200 空集；白名单外 404）。"""
    _customer_in_scope(customer_id)
    return service.entity_graph(db, datasets_db, customer_id=customer_id)


__all__ = ["SCOPE_ENV", "router"]
