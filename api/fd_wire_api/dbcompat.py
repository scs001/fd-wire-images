"""双方言（SQLite 开发 / PostgreSQL 生产）SQL 兼容层。

审计口径与逐项清单见
``openspec/changes/wire-dedicated-postgres/reports/dialect-audit.md``：

- **周期派生（YYYY-MM）**：SQLite ``strftime`` / PG ``to_char`` —— 一律经
  ``period_expression(bind, column)`` 取双方言表达式，不直接写 ``func.strftime``。
- **布尔聚合**：PG 无 ``max(boolean)``、``sum(boolean)`` —— 布尔计量一律
  ``count(*) FILTER (WHERE …)`` 或 ``sum(case((cond, 1), else_=0))``（整数化）。
- **JSON**：经 SQLAlchemy ``JSON`` 类型抽象读写，不写 ``json_extract`` 等
  原生 JSON1 函数（PG 侧为 ``json/jsonb`` 均可经该抽象）。
- **时间**：入库统一 naive-UTC 或 aware-UTC（各切片既有纪律），周期比较
  在派生列上进行，不做 ``julianday`` 类 SQLite 专有运算。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, func


def is_sqlite(bind: Any) -> bool:
    """目标方言是否 SQLite（bind 接受 Engine/Connection/Session）。"""
    return bind.dialect.name == "sqlite"


def period_expression(bind: Any, column: ColumnElement[Any]) -> ColumnElement[str]:
    """从时间列派生 ``YYYY-MM`` 周期串（SQLite strftime / PG to_char）。

    ``bind`` 是查询将要执行的引擎侧句柄（Engine / Connection / Session，
    Session 用 ``db.get_bind()`` 取）——方言在语句构建期选定，同一查询
    构建后不再跨方言复用。
    """
    if is_sqlite(bind):
        return func.strftime("%Y-%m", column)
    return func.to_char(column, "YYYY-MM")


__all__ = ["is_sqlite", "period_expression"]
