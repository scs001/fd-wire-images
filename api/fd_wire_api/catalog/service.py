"""目录查询服务：只读（经 db.readonly_session），供公开 API 消费。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, or_, select

from .db import readonly_session
from .models import IndicatorSnapshot


class CatalogQueryService:
    """镜像查询：搜索/过滤/分页/详情。没有也不提供任何写入方法。"""

    def __init__(self, session_factory=readonly_session):
        self._session_factory = session_factory

    def search(
        self,
        q: str | None = None,
        domain: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        page = max(1, page)
        page_size = min(max(1, page_size), 100)
        with self._session_factory() as session:
            stmt = select(IndicatorSnapshot)
            if status:
                stmt = stmt.where(IndicatorSnapshot.status == status.strip().lower())
            if domain:
                stmt = stmt.where(IndicatorSnapshot.domain == domain.strip())
            if q:
                needle = f"%{q.strip()}%"
                stmt = stmt.where(
                    or_(
                        IndicatorSnapshot.code.ilike(needle),
                        IndicatorSnapshot.name_zh.ilike(needle),
                        IndicatorSnapshot.name_en.ilike(needle),
                        IndicatorSnapshot.description_zh.ilike(needle),
                        IndicatorSnapshot.description_en.ilike(needle),
                    )
                )
            total = session.execute(
                select(func.count()).select_from(stmt.subquery())
            ).scalar_one()
            rows = (
                session.execute(
                    stmt.order_by(IndicatorSnapshot.domain, IndicatorSnapshot.code, IndicatorSnapshot.id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                .scalars()
                .all()
            )
        return {
            "items": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get(self, indicator_ref: str) -> IndicatorSnapshot | None:
        """按 indicator_id（权威主键）或镜像代理 id 取详情。"""
        ref = indicator_ref.strip()
        if not ref:
            return None
        with self._session_factory() as session:
            row = session.execute(
                select(IndicatorSnapshot).where(IndicatorSnapshot.indicator_id == ref)
            ).scalar_one_or_none()
            if row is None and ref.isdigit():
                row = session.get(IndicatorSnapshot, int(ref))
            if row is None:
                return None
            # 脱离只读 session 后仍可读字段（expire_on_commit=False + 属性已加载）
            return row

    def stats(self) -> dict[str, Any]:
        """目录计数（/v1/catalog/stats）：镜像 GROUP BY source_db 现算。

        ``registered`` 是登记总量口径（注册表全量，含 verified）；``verified``
        为其中已核验数。无缓存——同步完成后数值自然新鲜。
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    IndicatorSnapshot.source_db,
                    func.count().label("total"),
                    func.sum(
                        case(
                            (IndicatorSnapshot.status == "verified", 1),
                            else_=0,
                        )
                    ).label("verified"),
                ).group_by(IndicatorSnapshot.source_db)
            ).all()
        by_source = [
            {
                "source_db": row.source_db or "",
                "registered": int(row.total or 0),
                "verified": int(row.verified or 0),
            }
            for row in rows
        ]
        return {
            "registered": sum(b["registered"] for b in by_source),
            "verified": sum(b["verified"] for b in by_source),
            "by_source": by_source,
        }
