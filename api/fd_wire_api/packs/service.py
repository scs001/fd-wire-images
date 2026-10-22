"""源包服务：包计数/清单（目录镜像实时切片）+ 订阅读写（幂等）。

两套数据面刻意分开：
- **读目录**：包的指标数与清单按 filter 实时查 ``indicator_snapshots``
  （经 ``catalog.db.readonly_session``，只读 guard 生效）。口径与
  /v1/catalog/stats 的 registered 一致——**不筛 status，全量计数**
  （registered 口径＝登记总量，含 verified）。
- **写订阅**：``pack_subscriptions`` 落元数据库（路由传入请求级 session），
  (customer_id, pack_id) 唯一约束 + 先查后插，订阅/退订均幂等（重复操作
  不报错、不多写行）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..catalog.db import readonly_session
from ..catalog.models import IndicatorSnapshot
from .definitions import PACKS, PackDefinition
from .models import PackSubscription

#: 详情清单分页与 catalog /indicators 同界
_MAX_PAGE_SIZE = 100


def _filter_statement(model_stmt, filter_: dict):
    """把包 filter（{domains, source_dbs, codes} 任一组合）编译为目录查询条件。

    维度内取并集（IN），维度间取交集（AND）；空 filter = 全量目录。
    """
    domains = [d for d in (filter_.get("domains") or []) if d]
    source_dbs = [s for s in (filter_.get("source_dbs") or []) if s]
    codes = [c for c in (filter_.get("codes") or []) if c]
    if domains:
        model_stmt = model_stmt.where(IndicatorSnapshot.domain.in_(domains))
    if source_dbs:
        model_stmt = model_stmt.where(IndicatorSnapshot.source_db.in_(source_dbs))
    if codes:
        model_stmt = model_stmt.where(IndicatorSnapshot.code.in_(codes))
    return model_stmt


class PackQueryService:
    """包 → 目录切片的只读查询（无任何写入方法）。"""

    def __init__(self, session_factory=readonly_session):
        self._session_factory = session_factory

    def count(self, pack: PackDefinition) -> int:
        """包指标数（registered 口径＝镜像全量，含 verified）。"""
        with self._session_factory() as session:
            stmt = _filter_statement(select(func.count()).select_from(IndicatorSnapshot), pack.filter)
            return int(session.execute(stmt).scalar_one() or 0)

    def indicators(self, pack: PackDefinition, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        """包指标清单（分页；排序 domain, code, id 与目录搜索一致）。"""
        page = max(1, page)
        page_size = min(max(1, page_size), _MAX_PAGE_SIZE)
        with self._session_factory() as session:
            base = _filter_statement(select(IndicatorSnapshot), pack.filter)
            total = session.execute(
                select(func.count()).select_from(base.subquery())
            ).scalar_one()
            rows = (
                session.execute(
                    base.order_by(
                        IndicatorSnapshot.domain, IndicatorSnapshot.code, IndicatorSnapshot.id
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                .scalars()
                .all()
            )
        items = [
            {"code": row.code, "name_zh": row.name_zh, "domain": row.domain} for row in rows
        ]
        return {"items": items, "total": int(total), "page": page, "page_size": page_size}


class PackSubscriptionService:
    """订阅读写（元数据库 session 由路由层传入，请求级提交）。"""

    def list_pack_ids(self, db: Session, *, customer_id: str) -> list[str]:
        """我的订阅包 id 列表（订阅时间正序，先订在前）。"""
        rows = db.scalars(
            select(PackSubscription)
            .where(PackSubscription.customer_id == customer_id)
            .order_by(PackSubscription.id)
        ).all()
        return [row.pack_id for row in rows]

    def subscribe(self, db: Session, *, customer_id: str, pack_id: str) -> bool:
        """订阅（幂等）：已订阅返回 False，新订阅返回 True 并落行。"""
        existing = self._get(db, customer_id=customer_id, pack_id=pack_id)
        if existing is not None:
            return False
        db.add(PackSubscription(customer_id=customer_id, pack_id=pack_id))
        try:
            db.flush()
        except IntegrityError:
            # 并发下撞唯一约束：另一请求已写入——幂等吸收
            db.rollback()
            return False
        return True

    def unsubscribe(self, db: Session, *, customer_id: str, pack_id: str) -> bool:
        """退订（幂等）：删行返回 True，本就未订阅返回 False。"""
        result = db.execute(
            delete(PackSubscription).where(
                PackSubscription.customer_id == customer_id,
                PackSubscription.pack_id == pack_id,
            )
        )
        return bool(result.rowcount)

    def _get(self, db: Session, *, customer_id: str, pack_id: str) -> PackSubscription | None:
        return db.scalar(
            select(PackSubscription).where(
                PackSubscription.customer_id == customer_id,
                PackSubscription.pack_id == pack_id,
            )
        )


def list_packs_with_counts(query_service: PackQueryService | None = None) -> list[dict[str, Any]]:
    """包列表 + 各包实时指标数（顺序即 definitions.PACKS 展示序）。"""
    query_service = query_service or PackQueryService()
    return [
        {
            "id": pack.id,
            "name_zh": pack.name_zh,
            "name_en": pack.name_en,
            "desc": pack.desc,
            "indicator_count": query_service.count(pack),
        }
        for pack in PACKS
    ]
