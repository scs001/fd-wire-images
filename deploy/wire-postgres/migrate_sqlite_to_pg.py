#!/usr/bin/env python3
"""SQLite → PostgreSQL 生产数据搬运（wire-dedicated-postgres cutover 工具）。

按白名单逐表复制（列名对齐、类型感知、executemany 批量、分表事务），逐表输出
src/dst 计数，任一不齐即非零退出——「对账不过即止，不切 env」
（deploy/wire-postgres/README.md 生产迁移手册）。

用法：
  python3 migrate_sqlite_to_pg.py --sqlite /path/fd_wire_platform.db \
      --pg "postgresql+psycopg://wire:***@100.64.0.12:5433/wire_platform" \
      --tables billing_customers,billing_plans,...

类型映射（读 PG information_schema 反推）：
  boolean      ← sqlite int 0/1 → False/True
  jsonb/json   ← sqlite TEXT → ::jsonb 显式转型（空串转 NULL）
  timestamp/*  ← 原样字符串（PG 赋值语境接受 'YYYY-MM-DD HH:MM:SS[.ffffff]'）
  其余         ← 原样传递
"""
import argparse
import json
import sqlite3
import sys

import psycopg


def pg_column_types(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    ).fetchall()
    if not rows:
        raise SystemExit(f"PG 目标表不存在: {table}")
    return {name: dtype for name, dtype in rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", required=True)
    ap.add_argument("--pg", required=True)
    ap.add_argument("--tables", required=True, help="逗号分隔白名单")
    ap.add_argument("--skip-nonempty", action="store_true",
                    help="目标表已有行则跳过（幂等重跑用）")
    args = ap.parse_args()

    lite = sqlite3.connect(args.sqlite)
    lite.row_factory = sqlite3.Row
    # 接受 SQLAlchemy 形态 URL（postgresql+psycopg://…），psycopg 原生只认 postgresql://
    dsn = args.pg.replace("postgresql+psycopg://", "postgresql://", 1)
    # autocommit=True：否则 psycopg3 隐式事务会让下面的 transaction() 块退化成
    # savepoint、整进程退出即全量回滚（首跑踩坑实证——脚本内对账 OK、新连接全空）
    pg = psycopg.connect(dsn, autocommit=True)
    failures: list[str] = []

    for table in [t.strip() for t in args.tables.split(",") if t.strip()]:
        cols = [r[1] for r in lite.execute(f"PRAGMA table_info({table})")]
        if not cols:
            raise SystemExit(f"SQLite 源表不存在: {table}")
        pg_types = pg_column_types(pg, table)
        missing = [c for c in cols if c not in pg_types]
        if missing:
            raise SystemExit(f"{table}: PG 缺列 {missing}")
        existing = pg.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        if existing and args.skip_nonempty:
            print(f"SKIP {table}: pg 已有 {existing} 行（--skip-nonempty）")
            continue
        if existing and not args.skip_nonempty:
            raise SystemExit(f"{table}: 目标非空（{existing} 行），拒绝重复灌入；"
                             f"确认重灌请清表或用 --skip-nonempty")
        rows = [dict(r) for r in lite.execute(f"SELECT * FROM {table}")]
        src = len(rows)
        col_list = ",".join(f'"{c}"' for c in cols)
        # json/jsonb 列用显式 ::jsonb 转型（psycopg AUTO 占位符不吃 dict）
        ph = ",".join(
            "%s::jsonb" if pg_types[c] in ("json", "jsonb") else "%s"
            for c in cols
        )

        def _adapt(row: dict, pg_types=pg_types, cols=cols) -> list:
            vals = []
            for c in cols:
                v = row[c]
                dt = pg_types[c]
                if dt == "boolean" and isinstance(v, int):
                    v = bool(v)
                elif dt in ("json", "jsonb"):
                    v = v if isinstance(v, str) else json.dumps(v)
                    if v == "":
                        v = None
                vals.append(v)
            return vals

        # executemany 批量（psycopg3 pipeline）；分表事务——失败只回滚该表
        with pg.transaction():
            for i in range(0, src, 500):
                pg.cursor().executemany(
                    f'INSERT INTO "{table}" ({col_list}) VALUES ({ph})',
                    [_adapt(r) for r in rows[i : i + 500]],
                )
        dst = pg.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        ok = "OK " if src == dst else "FAIL"
        if src != dst:
            failures.append(table)
        print(f"{ok} {table}: sqlite={src} pg={dst}", flush=True)

    if failures:
        print(f"MISMATCH: {failures}", file=sys.stderr)
        return 1
    print("ALL TABLES RECONCILED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
