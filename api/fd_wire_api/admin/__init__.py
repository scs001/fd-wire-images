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

import importlib
from typing import Any

#: 名字 → 拥有它的子模块。**惰性导出**（PEP 562）：本包被 ``console.auth`` 间接拖进来，
#: 急切 ``from .routes import router`` 会把 ``admin.auth → console.auth`` 绕成环，
#: "先导入 ``ai.routes`` / ``billing.routes``"的冷启动直接 ImportError
#: （2026-11-13 由 ``tests/test_cold_imports.py`` 逐条抓出）。
_EXPORTS: dict[str, str] = {
    "AdminBase": ".db",
    "CatalogReviewNote": ".models",
    "get_db": ".db",
    "get_session": ".db",
    "init_db": ".db",
    "require_admin_token": ".auth",
    "reset_admin_runtime": ".db",
    "router": ".routes",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
