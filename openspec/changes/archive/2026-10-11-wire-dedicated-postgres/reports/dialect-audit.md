# 双方言审计清单（wire-dedicated-postgres 任务 3.1–3.3）

- 审计日期：2026-10-11
- 审计范围：`api/fd_wire_api/` 全部模块——**排除** `datasets/`、`ai/`、
  `downloads/`（并行代理正在改，见文末「并行边界」）；alembic 迁移链
  以 `alembic/env.py`（本批已加 `FD_WIRE_DATABASE_URL` 优先级）与既有
  versions 为准（versions 本批不动）。
- 排查方法：全量 grep SQLite 特有形态（`func.strftime` / `julianday` /
  `json_extract` / `AUTOINCREMENT` / `PRAGMA` / `randomblob` / 裸 DDL
  `text(...)` / `sqlite_insert` / `on_conflict`）+ 逐模型文件人工复核
  （布尔列参与聚合、JSON 列、时间列形态、自增主键形态）。
- 回归保障：`tests/test_pg_dialect_smoke.py`（全部模型 PG 方言
  create/drop 编译断言 + 双方言周期表达式编译断言；`PG_TEST_URL` 存在
  则真连建表+CRUD）+ 既有 SQLite 全量测试（559 passed）。

## 逐项清单

### dialect-01 周期派生 `func.strftime("%Y-%m", col)` —— 已修复

SQLite 专有函数，PG 无 `strftime`（PG 为 `to_char`）。命中 4 处，
统一收敛到 `fd_wire_api/dbcompat.py::period_expression(bind, column)`
（SQLite 渲染 `strftime` / PG 渲染 `to_char`，语句构建期按引擎方言选定）：

| 位置 | 用途 | 状态 |
|---|---|---|
| `billing/metering_source.py::SqlMeteringSource.aggregates` | 结算整月聚合 | 已修复 + 编译断言回归 |
| `billing/routes.py::monthly_used_lines` | 额度/控制台用量 | 已修复 |
| `materialization/quota.py::SqlQuotaGuard.check` | 物化硬停额度 | 已修复 |
| `admin/routes.py::customer_usage` | 内部客户用量分解 | 已修复 |

### dialect-02 裸 SQLite DDL `METERING_RECORDS_DDL` —— 已修复

`billing/metering_source.py` 的测试/临时环境建表 DDL 写死
`INTEGER PRIMARY KEY AUTOINCREMENT`（PG 语法错误）。修复：

- Core `Table("metering_records", …)` 补齐与 ORM 全列对齐
  （caller_principal/server_path/tool_name/created_at +
  `uq_metering_request_server` 唯一约束）；
- 新增 `ensure_metering_records_table(bind)`（`Table.create(checkfirst=True)`，
  SQLite/PG 双方言）；
- 三处测试引导（`tests/console_fixture.py`、`tests/test_admin_api.py`、
  `tests/test_billing_common.py`）改用之；
- 原 `METERING_RECORDS_DDL` 常量保留并注明 SQLite 专用（历史兼容）。

### dialect-03 布尔聚合（PG 无 `max(boolean)` / `sum(boolean)`）—— 无违例，约定沉淀

全量排查 `func.max` / `func.sum`：

- `func.max`：`provenance/routes.py`、`provenance/sealing.py`（`seq` 整数）、
  `metering/registry_poller.py`（`event_timestamp` datetime，PG 合法且
  已带 `_coerce` 折算 aware-UTC）——无布尔聚合。
- `func.sum`：`rows_returned`（int）与 `catalog/service.py::stats` 的
  `sum(case((status=='verified', 1), else_=0))` 整数化——合规范本。
- 布尔列（`billing.Plan.is_active/is_public`、`materialization.
  MaterializedArtifact.enabled`）均不参与聚合。

**约定**（dbcompat 模块头注沉淀）：新增布尔计量一律
`count(*) FILTER (WHERE …)` 或 `sum(case((cond, 1), else_=0))`，禁
`max(bool)`。

### dialect-04 JSON 列 —— 经 SQLAlchemy 抽象，无原生 JSON1 调用

JSON 列清单（均 `sqlalchemy.JSON`，无 `json_extract`/`json_each` 原生调用，
grep 零命中）：

| 模块 | 列 |
|---|---|
| demands | entities_hint / linked_indicators / status_history / evaluation_detail / quote_history / pipeline_events / delivery_events |
| billing | limits_json |
| materialization | spec / last_run_detail |
| provenance | actor / subject / payload（同 datasets 库） |

SQLite 侧渲染 TEXT(JSON)、PG 侧经既有迁移链为 `json` 类型——双方言读写
成立。JSONB 升级为迁移后可选优化（deploy/wire-postgres/README「五」已给
`ALTER ... TYPE jsonb USING ...::jsonb`；ORM 不改类型避免与冻结迁移链漂移）。

### dialect-05 时间函数与精度 —— 复核通过

- `server_default=func.now()`（metering 两表）双方言合法；
  `CURRENT_TIMESTAMP`（DDL）同。
- 切片纪律一致未混用：metering/billing/admin=aware-UTC（`utc_now`/`utcnow`），
  demands/materialization/provenance=naive-UTC（`_utcnow_naive`）；
  catalog=`UTCDateTime` TypeDecorator（SQLite naive+读回补 UTC /
  PG `timestamptz`，load_dialect_impl 双分支——已是范本）。
- 周期比较一律在派生串（YYYY-MM）上进行，无 `julianday` 类专有运算。

### dialect-06 自增/序列形态 —— 通过

全部自增主键为 `Integer, primary_key=True[, autoincrement=True]` 经
SQLAlchemy 抽象（SQLite=INTEGER PRIMARY KEY rowid / PG=SERIAL+序列）；
PG 编译断言确认输出无 `AUTOINCREMENT` 字面量。

### dialect-07 只读镜像 guard —— 已双方言（无需改）

`catalog/db.py`：连接级 `PRAGMA query_only` / PG
`SET default_transaction_read_only`，语句级写动词表含 PG 词（COPY/VACUUM/
GRANT…），ORM flush guard——原设计即双方言。

### dialect-08 不可变触发器 —— 已双方言（无需改）

`provenance/__init__.py::_install_immutable_triggers`：SQLite 拆
update/delete 双触发器 `RAISE(ABORT)`；PG plpgsql 函数 + 组合触发器。
（alembic 0012 同构，迁移链侧不在本批范围。）

### dialect-09 LIKE 大小写语义 —— 已评估无需改

- `catalog/service.py` 搜索用 `.ilike(...)`：SQLAlchemy 抽象
  （SQLite 渲染 `lower() LIKE lower()` / PG 渲染 `ILIKE`），口径一致。
- `demands/service.py` 用 `Demand.code.like(prefix + "%")`：code 恒为
  `DE-` 大写形态（SQLite LIKE 对 ASCII 大小写不敏感 / PG LIKE 敏感），
  前缀与列值同为大写常量域，两方言结果一致——不改；若未来对小写敏感
  域用 `like` 需复查。

### dialect-10 alembic `render_as_batch=True` —— 通过

batch 模式是 SQLite 重建表专用参数，PG 方言下静默忽略，无害。

### dialect-11 free-tier-data-gate 新表（并行代理负责）—— 编译断言已覆盖

`dataset_columns` / `dataset_rows`（api 层行存储）源码归并行代理（其表
设计自带双方言约束）；本批已把 `DatasetsBase.metadata` 纳入
`test_pg_dialect_smoke.py` 的 PG create/drop 编译断言——新表自动受检，
集成时主代理复核真连冒烟（`PG_TEST_URL`）。

## 并行边界记录

- `datasets/`、`ai/`、`downloads/` 源码本批未动：datasets 的
  `FD_WIRE_DATASETS_DATABASE_URL` env 名与收敛语义一致（无需改名，测试
  断言兼容）；ai 切片保持独立 `AI_DATABASE_URL`（生产迁移手册已注明与
  `FD_WIRE_DATABASE_URL` 同值指向 wire_platform）；PG URL 规范化
  （`postgresql+psycopg://` 改写）在 datasets/ai 引擎的接入由各自并行
  代理完成，集成时主代理复核。
- 并行代理同窗口落盘 alembic 0020/0021 迁移，五处既有「链头=0019」断言
  过期，本批机械更新为 0021（test_ai_infra / test_catalog_migrations /
  test_datasets_migrations / test_demands_pipeline_delivery /
  test_demands_three_state_migration）。
