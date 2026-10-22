"""内部后台切片（wire-platform-v1 任务 7.1）：``/internal/admin/*``。

一面五台（鉴权一律 ``X-Admin-Token`` ↔ env ``FD_WIRE_ADMIN_TOKEN``，未配置
fail-closed 403）：
- 客户管理（billing_customers/billing_plans：只读查询 + 开户记录，
  sub2api 代建留给后续）
- 额度与账本（metering_records 只读联查 / billing_ledger_entries 只读）
- 需求单派发台（全客户列表 + 状态机迁移，直调 demands service 层）
- 目录治理（镜像只读 + catalog_review_notes append-only 审核意见，
  alembic 0007）
- 用量分轨概览（credential_track × source 聚合）
"""

from __future__ import annotations

from .auth import require_admin_token
from .db import AdminBase, get_db, get_session, init_db, reset_admin_runtime
from .models import CatalogReviewNote
from .routes import router

__all__ = [
    "AdminBase",
    "CatalogReviewNote",
    "get_db",
    "get_session",
    "init_db",
    "require_admin_token",
    "reset_admin_runtime",
    "router",
]
