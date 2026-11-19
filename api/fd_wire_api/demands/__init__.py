"""demands 包：数据需求单三态状态机（wire-ux-consolidation 任务 3.1，
自 wire-platform-v1 六任务组的八态工作流压缩而来）+ 新单邮件通知（3.3）
+ 流水线衔接与私有 scope 交付（6.5/6.6，前置态改为 in_progress）。

现行状态机：submitted → in_progress → delivered，另 rejected（终态，必带
理由）；evaluating/quoted/awaiting_confirm/building/delivery_acceptance
五个旧态废除——不可迁入（409），存量由 alembic 0019 归并为 in_progress。

对外暴露 ``router``（客户/内部核心端点）、``admin_router``（内部完整视图）
与 ``pipeline_router``（流水线事实、gate 结果、交付建 scope、授权转公开），
挂到 app 即得全部端点。
"""

from .admin_routes import router as admin_router
from .db import Base, engine, get_db, init_db
from .evaluation import (
    LEGAL_DOMAIN_REVIEW_FLAG,
    EvaluationResult,
    EvaluationVerdict,
    evaluate_demand,
)
from .models import Demand
from .notifications import notify_new_demand
from .pipeline_routes import router as pipeline_router
from .routes import router
from .statemachine import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    LEGACY_STATUSES,
    TERMINAL_STATUSES,
    DemandStatus,
    TransitionError,
)

__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_TRANSITIONS",
    "LEGAL_DOMAIN_REVIEW_FLAG",
    "LEGACY_STATUSES",
    "TERMINAL_STATUSES",
    "Base",
    "Demand",
    "DemandStatus",
    "EvaluationResult",
    "EvaluationVerdict",
    "TransitionError",
    "admin_router",
    "engine",
    "evaluate_demand",
    "get_db",
    "init_db",
    "notify_new_demand",
    "pipeline_router",
    "router",
]
