"""平台自营发布方（wire-showcase-market）。

平台自身作为一个普通客户租户存在（``billing_customers`` 的一行），其名下产物
视为平台自有资产、可经匿名橱窗对外。判定口径只有一条：**归属租户名等于
``PLATFORM_PUBLISHER_NAME``**。

**库归属**：``billing_customers`` 住在**平台库**（与 ``console_*`` 同库，经
``admin.db`` 引擎），而 ``artifacts`` 住在 **ai 库**。两者不是同一个会话——
本模块的函数因此显式要求调用方传入**平台库会话**，绝不接受 artifacts 会话
（在 artifacts 会话上查 billing 表会 ``no such table``，而这个错误在生产上
只会在第一次匿名访问时炸出来）。

为什么用 ``name`` 而不是新加一列：``billing_customers.name`` 已有全表唯一索引
（迁移 0037），且它正是客户面所有数据（datasets/artifacts/ai）的
``customer_id`` 键——用它判定不需要迁移、不需要回填，也不会与既有键产生第二个
真源。代价是「名字」在语义上可能被运维改动，因此判定收敛在本模块的两个符号里
（常量 + helper），改名单点可查，并有测试锁死常量值。

安全默认：租户行缺失时 ``resolve_platform_publisher`` 返回 None，调用方（匿名
橱窗端点）按「无平台发布方」处理并返回空列表——宁可橱窗空，绝不让服务 500，
也绝不因缺失而放宽任何过滤条件。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..billing.models import Customer

#: 平台自营发布方的客户租户名（也是其 artifacts.customer_id 值）。
#: 改动此常量必须同步数据行与测试（单点可查是本设计的一部分）。
PLATFORM_PUBLISHER_NAME = "finddata-official"

#: 平台自营发布方展示名（市场徽标/橱窗页脚用；不参与任何判定）。
PLATFORM_PUBLISHER_DISPLAY_NAME = "寻数官方"


def resolve_platform_publisher(platform_db: Session) -> Customer | None:
    """返回平台自营发布方的客户行；不存在返回 None（安全默认，不抛错）。

    ``platform_db`` MUST 是平台库会话（``admin.db.get_db`` / 同一引擎）——
    传 artifacts 的 ai 库会话会因缺表而报错。
    """
    return platform_db.execute(
        select(Customer).where(Customer.name == PLATFORM_PUBLISHER_NAME)
    ).scalar_one_or_none()


def is_platform_publisher(customer_id: str | None, platform_db: Session) -> bool:
    """``customer_id`` 是否为平台自营发布方。

    **只按标识判定，绝不看产物标题/标签等可伪造字段**——这是橱窗红线成立的
    前提（否则任何客户改个标题就能把自己推进匿名面）。

    先做零查询的字符串比对：绝大多数调用（客户产物）在此直接返回 False，
    只有标识吻合时才回库确认租户真实存在（防止伪造 customer_id 的产物绕过）。
    """
    if not customer_id or customer_id != PLATFORM_PUBLISHER_NAME:
        return False
    return resolve_platform_publisher(platform_db) is not None


def ensure_platform_publisher(platform_db: Session, *, plan_code: str = "free") -> Customer:
    """幂等地建立/取回平台租户行（导入器与引导脚本共用）。

    name 冲突即复用既有行（不新建）；plan 缺失时抛错让调用方显式处理——
    静默挂错档位会比报错更难排查。
    """
    from ..billing.models import Plan

    existing = resolve_platform_publisher(platform_db)
    if existing is not None:
        return existing
    plan = platform_db.execute(
        select(Plan).where(Plan.code == plan_code)
    ).scalar_one_or_none()
    if plan is None:
        raise RuntimeError(
            f"平台租户引导失败：找不到档位 {plan_code}（先跑 plans seed）"
        )
    customer = Customer(name=PLATFORM_PUBLISHER_NAME, plan_id=plan.id, status="active")
    platform_db.add(customer)
    platform_db.flush()
    return customer


__all__ = [
    "PLATFORM_PUBLISHER_DISPLAY_NAME",
    "PLATFORM_PUBLISHER_NAME",
    "ensure_platform_publisher",
    "is_platform_publisher",
    "resolve_platform_publisher",
]
