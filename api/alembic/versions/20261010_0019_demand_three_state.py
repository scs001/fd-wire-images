"""demand three-state consolidation（wire-ux-consolidation 3.2+3.4）

- 新列：``contact_email`` / ``company``——意向单表单收窄（3.2）后的联系
  方式落库；旧单无值，可空起步。
- 存量归并（3.4，一次性 + 幂等）：
  * 五个废除态（evaluating/quoted/awaiting_confirm/building/
    delivery_acceptance）全部归并为 ``in_progress``；
  * delivered / rejected 状态不动；
  * ``status_history`` 留痕：追加一条归并条目（by=migration），并把历史
    条目中的废除态名重映射为 ``in_progress``（连续同态去重、保首条），
    使 API 响应不再出现废除态名（归并条目文案亦不含旧态名）。

幂等语义：:func:`merge_legacy_demand_statuses` 只处理仍处于废除态的行
（重跑无匹配行 → 无状态再写、无新事件）；历史重映射与去重是幂等变换
（重跑结果稳定）。alembic 版本表本身保证 upgrade 只执行一次，函数级
幂等为直接重跑/补跑提供同等保证。

Revision ID: 0019_demand_three_state
Revises: 0018_plan_limits
Create Date: 2026-10-10

"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_demand_three_state"
down_revision: Union[str, None] = "0018_plan_limits"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None

#: 五个废除态（与 fd_wire_api.demands.statemachine.LEGACY_STATUSES 对表维护）。
LEGACY_STATUSES: tuple[str, ...] = (
    "evaluating",
    "quoted",
    "awaiting_confirm",
    "building",
    "delivery_acceptance",
)

#: 归并目标态。
MERGED_STATUS = "in_progress"

#: 归并留痕条目（append-only 追加到 status_history 末尾；不含旧态名）。
_MERGE_MARKER_NOTE = "状态机三态化归并：废除工作流态并入 in_progress（wire-ux-consolidation 3.4）"
_MERGE_MARKER_BY = "migration:0019_demand_three_state"

#: 轻量表构造（数据迁移标准姿势：不 import ORM，跨版本自洽）。
_demands = sa.table(
    "demands",
    sa.column("id", sa.Integer),
    sa.column("status", sa.String),
    sa.column("status_history", sa.JSON),
)


def _load_history(value: object) -> list:
    """兼容各方言的 status_history 读值（list 或 JSON 文本均可）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _remap_history(history: list) -> tuple[list, bool]:
    """废除态名 → in_progress + 连续同态去重（保首条）。

    返回 (新列表, 是否发生变更)。幂等：对已归并的历史重跑结果稳定。
    """
    out: list = []
    changed = False
    for entry in history:
        if isinstance(entry, dict) and entry.get("status") in LEGACY_STATUSES:
            entry = {**entry, "status": MERGED_STATUS}
            changed = True
        if (
            out
            and isinstance(out[-1], dict)
            and isinstance(entry, dict)
            and out[-1].get("status") == entry.get("status")
        ):
            continue  # 连续同态去重：保首条（时间线仍单调）
        out.append(entry)
    return out, changed


def merge_legacy_demand_statuses(conn: sa.Connection) -> int:
    """一次性归并五废除态 → in_progress（幂等，返回归并行数）。

    - 仍处废除态的行：状态改写 + 历史重映射 + 追加归并留痕条目；
    - 其余行（含 delivered/rejected/submitted/in_progress）：状态不动，
      仅当历史含废除态名时做重映射（不追加事件——状态未被归并）。
    """
    merged = 0

    # 1) 废除态行：状态归并 + 留痕（重跑无匹配行，天然幂等）
    rows = conn.execute(
        sa.select(_demands.c.id, _demands.c.status_history).where(
            _demands.c.status.in_(LEGACY_STATUSES)
        )
    ).all()
    for demand_id, history_value in rows:
        history, _ = _remap_history(_load_history(history_value))
        history.append(
            {
                "status": MERGED_STATUS,
                "at": datetime.now(timezone.utc).isoformat(),
                "by": _MERGE_MARKER_BY,
                "note": _MERGE_MARKER_NOTE,
            }
        )
        conn.execute(
            _demands.update()
            .where(_demands.c.id == demand_id)
            .values(status=MERGED_STATUS, status_history=history)
        )
        merged += 1

    # 2) 非废除态行：只清洗历史中的旧态名（delivered/rejected 由此"不动状态"）
    other_rows = conn.execute(
        sa.select(_demands.c.id, _demands.c.status_history).where(
            sa.not_(_demands.c.status.in_(LEGACY_STATUSES))
        )
    ).all()
    for demand_id, history_value in other_rows:
        history, changed = _remap_history(_load_history(history_value))
        if changed:
            conn.execute(
                _demands.update()
                .where(_demands.c.id == demand_id)
                .values(status_history=history)
            )
    return merged


def upgrade() -> None:
    op.add_column("demands", sa.Column("contact_email", sa.String(length=320), nullable=True))
    op.add_column("demands", sa.Column("company", sa.String(length=200), nullable=True))
    merge_legacy_demand_statuses(op.get_bind())


def downgrade() -> None:
    # 状态归并是一次性收窄（历史不可逆推），downgrade 只撤新列。
    op.drop_column("demands", "company")
    op.drop_column("demands", "contact_email")
