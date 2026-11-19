"""知识档位一等字段（add-entity-bench-contract 任务 1.1 / design D1、D2）。

**纯增量**：``entities`` / ``entity_relationships`` 各加 ``tier`` /
``sensitivity`` 两列（String(16) nullable；null=未标注），加列沿 0042
``_add_missing`` 幂等先例。

回填（design D2「只升不删」）：扫描 ``entities.column_notes`` 的
``_bench`` 元数据袋（知验台 wire-connector 旧格式回写标记，分号分隔
``k=v`` 串——``platform=knowledge-bench; tier=gold; sensitivity=internal;
…``），把其中的 ``tier``/``sensitivity`` 升为一等字段：

- 仅当列现值为 NULL 且解析值在词表内才写（重跑零新增变更＝幂等）；
- 词表外/残缺标记跳过计数（不造假、不清袋）；
- **袋内容逐字保留**（连接器旧格式读法兼容承诺）。

``entity_relationships`` 无袋（旧格式对关系回写本就拒绝）——只加列不回填。

双方言（sqlite + PG）：JSON 列经裸 ``sa.text`` 读出在 PG 是 dict、在
sqlite 是 TEXT JSON 串，``_load_json`` 双形态归一。

Revision ID: 0050_entity_bench_tier_sensitivity
Revises: 0049_ontology_versioning
Create Date: 2026-11-05
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0050_entity_tier_sensitivity"
down_revision: Union[str, None] = "0049_ontology_versioning"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None

#: 词表（与 service 层常量逐字对齐；迁移不 import 应用层模块，钉死副本）
TIERS = ("raw", "bronze", "silver", "gold")
SENSITIVITIES = ("public", "internal", "confidential")
#: 知验台 wire-connector 的袋键与 stamp 前缀（writeback.BENCH_NOTE_KEY）
BENCH_NOTE_KEY = "_bench"
BENCH_STAMP_PLATFORM = "platform=knowledge-bench"


def _has_column(table: str, column: str) -> bool:
    return any(
        col["name"] == column
        for col in sa.inspect(op.get_bind()).get_columns(table)
    )


def _add_missing(table: str, columns: list[sa.Column]) -> None:
    existing = {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table)}
    for column in columns:
        if column.name not in existing:
            with op.batch_alter_table(table) as batch:
                batch.add_column(column)


def _load_json(value: object) -> dict | None:
    """JSON 列双方言归一：PG 原生 dict / sqlite TEXT JSON 串。"""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _parse_bench_stamp(stamp: object) -> tuple[str | None, str | None]:
    """``_bench`` 袋值 → (tier, sensitivity)；非本平台 stamp 或词表外值一律
    (None, None)——跳过如实，不猜测。"""
    if not isinstance(stamp, str) or BENCH_STAMP_PLATFORM not in stamp:
        return None, None
    fields: dict[str, str] = {}
    for part in stamp.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key:
            fields[key.strip()] = value.strip()
    tier = fields.get("tier")
    sensitivity = fields.get("sensitivity")
    return (
        tier if tier in TIERS else None,
        sensitivity if sensitivity in SENSITIVITIES else None,
    )


def upgrade() -> None:
    _add_missing(
        "entities",
        [
            sa.Column("tier", sa.String(length=16), nullable=True),
            sa.Column("sensitivity", sa.String(length=16), nullable=True),
        ],
    )
    _add_missing(
        "entity_relationships",
        [
            sa.Column("tier", sa.String(length=16), nullable=True),
            sa.Column("sensitivity", sa.String(length=16), nullable=True),
        ],
    )

    # ── 回填：袋标记升一等（幂等——只填 NULL 列，词表外跳过）────────────
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, tier, sensitivity, column_notes FROM entities")
    ).mappings().all()
    backfilled = 0
    skipped = 0
    for row in rows:
        notes = _load_json(row["column_notes"])
        stamp = (notes or {}).get(BENCH_NOTE_KEY)
        if stamp is None:
            continue
        tier, sensitivity = _parse_bench_stamp(stamp)
        if tier is None and sensitivity is None:
            skipped += 1
            continue
        updates: dict[str, object] = {"id": row["id"]}
        if tier is not None and row["tier"] is None:
            updates["tier"] = tier
        if sensitivity is not None and row["sensitivity"] is None:
            updates["sensitivity"] = sensitivity
        if "tier" not in updates and "sensitivity" not in updates:
            continue  # 列已有值（重跑）——幂等跳过，零写
        bind.execute(
            sa.text(
                "UPDATE entities SET tier = :tier, sensitivity = :sensitivity "
                "WHERE id = :id"
            ),
            {
                "id": row["id"],
                "tier": updates.get("tier", row["tier"]),
                "sensitivity": updates.get("sensitivity", row["sensitivity"]),
            },
        )
        backfilled += 1
    # 回填计数只进日志不造假数据——零标记库重跑静默零写（幂等语义）
    print(f"[0050] bench-stamp backfill: backfilled={backfilled} skipped={skipped}")


def downgrade() -> None:
    # 回填不可逆（升列值来源袋仍在，但降级丢一等语义）——如实只删列
    with op.batch_alter_table("entity_relationships") as batch:
        batch.drop_column("sensitivity")
        batch.drop_column("tier")
    with op.batch_alter_table("entities") as batch:
        batch.drop_column("sensitivity")
        batch.drop_column("tier")
