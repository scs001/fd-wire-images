"""管线图路由壳（wire-transform-pipeline-view 任务 1.1/1.2）。

薄壳：身份（console 会话 ``require_console_customer_id``）+ datasets
会话依赖（与 datasets 客户面同款 ``get_db``），组装逻辑全在 ``graph``
（便于直测）。零引擎调用——不存在 EngineToolClient 依赖。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..console.auth import require_console_customer_id
from ..datasets.db import get_db
from .graph import build_pipeline_graph

router = APIRouter(prefix="/api/v1/pipeline", tags=["pipeline"])


@router.get("/graph")
def get_pipeline_graph(
    customer_id: str = Depends(require_console_customer_id),
    db: Session = Depends(get_db),
) -> dict:
    """本企业数据集派生图（跨企业隔离在读取面：查询按 customer_id 过滤）。"""
    return build_pipeline_graph(db, customer_id)
