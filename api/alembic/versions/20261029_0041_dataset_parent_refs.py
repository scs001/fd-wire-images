"""派生数据集父集边表 + 存量回填（wire-sensitivity-propagation 任务 1.1）。

``dataset_parent_refs``：有效敏感度祖先闭包的唯一数据源（零引擎调用）。

回填口径（spec「派生父集登记与存量回填」，不猜纪律）：
- ``source_kind='derived'`` 且 ``source_ref`` 指向同客户存在数据集 key 的行 →
  回填单亲边（origin=legacy）——``source_ref`` 在派生行上即父集 key
  （_register_derived_child 既有语义）。
- **legacy link 的右父平台侧无记录**（本地血缘只记 source_ref 单亲）→ 不回填，
  闭包如实标记父集不全、设档闸 fail-closed；重新物化（重派生）即修复。
- 同 (child, parent) 幂等（唯一约束 + NOT EXISTS 防重；重复执行零重复行）。

测试同形 SQL 断言见 ``api/tests/test_sensitivity_propagation.py::test_backfill_*``；
本迁移与该测试的 SQL 必须保持逐字一致（改动须两边同改）。

Revision ID: 0041_dataset_parent_refs
Revises: 0040_artifact_source_ref
Create Date: 2026-10-29

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041_dataset_parent_refs"
down_revision: Union[str, None] = "0040_artifact_source_ref"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 存量回填（与 test_sensitivity_propagation.py 的 BACKFILL_SQL 逐字一致）
BACKFILL_SQL = (
    "INSERT INTO dataset_parent_refs "
    "(customer_id, child_dataset_id, parent_dataset_id, origin, created_at) "
    "SELECT c.customer_id, c.id, p.id, 'legacy', CURRENT_TIMESTAMP "
    "FROM datasets c JOIN datasets p "
    "ON p.customer_id = c.customer_id AND p.key = c.source_ref "
    "WHERE c.source_kind = 'derived' "
    "AND c.source_ref IS NOT NULL AND c.source_ref <> '' "
    "AND NOT EXISTS (SELECT 1 FROM dataset_parent_refs r "
    "WHERE r.child_dataset_id = c.id AND r.parent_dataset_id = p.id)"
)


def upgrade() -> None:
    # 生产开着 create_all 兜底（AUTO_CREATE_TABLES），模型建表可能已抢先建出
    # 该表与等价索引（索引名不同：ix_dataset_parent_refs_*）。按对象是否存在
    # 分支，使迁移在「先建表后跑迁移」与「干净库直跑迁移」两种顺序下都成立。
    inspect = sa.inspect(op.get_bind())
    table_exists = "dataset_parent_refs" in inspect.get_table_names()
    if not table_exists:
        op.create_table(
            "dataset_parent_refs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("customer_id", sa.String(length=64), nullable=False),
            sa.Column("child_dataset_id", sa.String(length=36), nullable=False),
            sa.Column("parent_dataset_id", sa.String(length=36), nullable=False),
            sa.Column(
                "origin", sa.String(length=16), nullable=False, server_default="derive"
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "child_dataset_id",
                "parent_dataset_id",
                name="uq_parent_refs_child_parent",
            ),
        )
    # 索引按「列是否已被某个索引覆盖」判定，而不是按索引名——create_all 建出的
    # 同名语义索引叫 ix_dataset_parent_refs_<col>，重复建只是冗余不是错，但没必要。
    # 重新取 inspector：上面若刚建表，旧快照看不到新表。
    inspect = sa.inspect(op.get_bind())
    covered = {tuple(ix["column_names"]) for ix in inspect.get_indexes("dataset_parent_refs")}
    if ("customer_id",) not in covered:
        op.create_index(
            "ix_parent_refs_customer", "dataset_parent_refs", ["customer_id"]
        )
    if ("parent_dataset_id",) not in covered:
        op.create_index(
            "ix_parent_refs_parent", "dataset_parent_refs", ["parent_dataset_id"]
        )
    # 存量回填（反向遍历需要 parent 索引——上面已建；SQL 自带 NOT EXISTS 幂等）
    op.get_bind().execute(sa.text(BACKFILL_SQL))


def downgrade() -> None:
    op.drop_table("dataset_parent_refs")
