"""quality rules（wire-quality-rules）：客户自定义质量规则 + 求值结果。

规则引擎设计（design D1-D5）：借 SodaCL 的 DSL 语法形（指标+列+操作符+阈值，
存结构化 JSON）与 Great Expectations 的结果契约（element_count /
unexpected_count / pass_rate），自实现编译器把每条规则翻译成引擎
``customer_dataset_query`` 的 ``count_only`` 计数查询——引擎侧零改动，
平台侧只见聚合计数（红线 2）。

新表两张（均挂 DatasetsBase，与 quality/datasets 切片同库）：

- ``dataset_quality_rules``：规则**配置**（可编辑，非 append-only——改阈值/
  启停/删除是正常操作；每次变更记 ``updated_by``）。``check_json`` 是封闭
  枚举的结构化检查体，写入前经 DSL 校验（design D3 注入防线）。
- ``dataset_quality_rule_results``：求值**结果**（append-only，同快照纪律：
  不存在任何 UPDATE 路径与改/删端点），每规则保留 ``basis_at``（求值基准
  时刻——snapshot 通道如实披露结论依据的快照年龄）。

注意：``enabled`` 默认 **true**（新加规则立即参与求值；布尔默认必须
``sa.text("true")``——``"0"/"1"`` 写法在 PG 会被拒，见 team-spaces 教训）。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035_quality_rules"
down_revision: Union[str, None] = "0034_dataset_marketplace"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None


def upgrade() -> None:
    op.create_table(
        "dataset_quality_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("check_json", sa.JSON(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_quality_rules")),
    )
    op.create_index(
        "ix_dataset_quality_rules_dataset_id",
        "dataset_quality_rules",
        ["dataset_id"],
    )

    op.create_table(
        "dataset_quality_rule_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        # pass | fail | error
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("element_count", sa.BigInteger(), nullable=True),
        sa.Column("unexpected_count", sa.BigInteger(), nullable=True),
        sa.Column("pass_rate", sa.Float(), nullable=True),
        sa.Column("observed", sa.Float(), nullable=True),
        sa.Column("threshold_json", sa.JSON(), nullable=True),
        # query | snapshot
        sa.Column("channel", sa.String(length=8), nullable=False),
        sa.Column("basis_at", sa.DateTime(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        # create | sync | materialize | manual（与快照同集）
        sa.Column("triggered_by", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_dataset_quality_rule_results")
        ),
    )
    op.create_index(
        "ix_dataset_quality_rule_results_rule_id",
        "dataset_quality_rule_results",
        ["rule_id"],
    )
    op.create_index(
        "ix_dataset_quality_rule_results_dataset_id",
        "dataset_quality_rule_results",
        ["dataset_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dataset_quality_rule_results_dataset_id",
        table_name="dataset_quality_rule_results",
    )
    op.drop_index(
        "ix_dataset_quality_rule_results_rule_id",
        table_name="dataset_quality_rule_results",
    )
    op.drop_table("dataset_quality_rule_results")
    op.drop_index(
        "ix_dataset_quality_rules_dataset_id", table_name="dataset_quality_rules"
    )
    op.drop_table("dataset_quality_rules")
