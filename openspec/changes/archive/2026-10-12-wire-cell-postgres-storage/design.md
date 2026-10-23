# Design: wire-cell-postgres-storage

## 1. 现状与侦察结论（2026-10-12）

- 引擎数据访问统一走 `daas_database.get_database()` 的 SQLAlchemy engine
  （`DAAS_DATABASE_URL` 任意 URL）；裸 SQL 经 `engine.raw_connection()`
  的 cursor 执行，`_tx()` 手工 `BEGIN IMMEDIATE … COMMIT`。
- cell entrypoint：`DAAS_DATABASE_URL="${DAAS_DATABASE_URL:-sqlite:///${DATA_DIR}/daas.db}"`
  ——env 覆盖通道已在，零 entrypoint 改动。
- SQLite 专属面（全量盘点，共五类）：
  1. `?` 占位符（约 40+ 执行点，散布 `_tx` 体内与 `_raw_query_*`）；
  2. `_tx()` 的 `BEGIN IMMEDIATE` 与 `conn.isolation_level = None`（sqlite3 API）；
  3. `PRAGMA table_info(…)` 两处（WAL 列漂移自检、数据集表内省）；
  4. `cell_event_wal` DDL 的 `INTEGER PRIMARY KEY AUTOINCREMENT`；
  5. `_raw_query_timed` 的 `set_progress_handler` 超时护栏 +
     `sqlite3.OperationalError` 捕获。
- 已核对 PG 兼容项：列类型集 `{INTEGER, REAL, TEXT}`；ORM 模型全部可移植
  类型（JSON/Text/Integer/DateTime/Boolean）；`UPDATE … FROM`、
  `CREATE TABLE … AS SELECT`、`INSERT … SELECT … WHERE NOT EXISTS`、
  `ALTER TABLE … RENAME TO`、`DROP TABLE IF EXISTS` 两方言同形；
  `__row_hash` 为 Python 侧计算（link 子集复用左表哈希列），无 SQL 标量
  函数依赖。

## 2. 方案：单一方言适配层（不重写为 ORM 查询）

新增 `customer-dataset-mcp/db_dialect.py`，全部方言差异收敛于此：

- `is_postgres() -> bool`：由 `get_database().engine.url.dialect` 派生，
  模块级缓存；sqlite 路径完全短路（不 import psycopg）。
- `wrap_cursor(cur)`：游标代理，`execute(sql, params)` 时——
  - PG：`sql.replace("?", "%s")`（仓库内 SQL 文本无含 `?` 的字符串字面量；
    以静态扫描断言+矩阵兜底）；
  - `PRAGMA table_info(<tbl>)` → 改写为 information_schema 查询并**回填
    sqlite 形状行**（位置 1=列名），两处调用点零改动；
  - sqlite：透传，行为字节不变。
- `_tx()` 分支：sqlite 维持 `BEGIN IMMEDIATE`（写锁语义保单写者）；PG
  走 `BEGIN`（并发控制交 PG 锁管理器）。
- `_raw_query_timed` 分支：sqlite 用 progress_handler（现状）；PG 在执行前
  `SET statement_timeout = <ms>`、后还原，超时映射为**同文案**域错误
  （「查询超时（>Ns）…」）。
- `cell_event_wal` DDL 模板按方言二选一：sqlite
  `INTEGER PRIMARY KEY AUTOINCREMENT` / PG `BIGINT GENERATED ALWAYS AS
  IDENTITY PRIMARY KEY`（插入不携带 seq，自增语义两方一致）。
- 异常映射：`sqlite3.OperationalError`（busy/locked）↔ psycopg
  锁类错误（`LockNotAvailable` / `SerializationFailure` 等）在既有捕获点
  归一。
- 参数类型严格度：`_coerce` 已按声明列类型产出 Python 值；PG 侧 psycopg
  按 Python 类型适配。矩阵实跑暴露的强制度差异逐点在 `_coerce` 内收口
  （sqlite 路径不感知——修正只可能让值更规范，行哈希在强制后计算，两后端
  一致）。

**不动面**：daas-mcp 其余工具组、謙面本地产品路径、api/web、entrypoint、
healthcheck。

## 3. 测试矩阵

- `tests/conftest.py` 按 `FD_ENGINE_TEST_BACKEND`（`sqlite`|`postgres`）
  选择数据根 URL；PG URL 来自 `FD_ENGINE_PG_TEST_URL`（一次性测试库，
  每次运行重建 schema），未设时 PG 腿跳过并在门禁层视为未跑（发布门要求
  两腿都跑过）。
- 三个 customer-dataset 测试文件全量双后端执行（不按后端裁剪用例）；
  新增双方言一致性用例：占位符字面量扫描、PG 严格类型强制（TEXT 列收
  int 等）、超时护栏文案、WAL 自增连续性。
- sqlite 腿为**回归零变化**腿：任何 sqlite 断言/行为的改动都不可接受。

## 4. 部署配方与试点

- 配方文档 `docs/cell-postgres-storage.md`（cheap-3 wire PG 实例，
  与 wire_platform/wire_datasets 同实例不同库）：
  1. `CREATE ROLE wire_cell_<key> PASSWORD '…'`（nologin 场景不适用，
     cell 需登录；LOGIN + 仅本库属主）；
  2. `CREATE DATABASE wire_cell_<key> OWNER wire_cell_<key>`；
  3. `REVOKE CONNECT ON DATABASE wire_cell_<key> FROM PUBLIC`；
     平台角色（wire）不授予；属主默认仅本库；
  4. 隔离验证 SQL（api 凭据连 cell 库→拒；属主越库→拒）；
  5. cell env 注入 `DAAS_DATABASE_URL`（cheap-1 env-file 0600，与
     wgk- 同纪律）；
  6. 回退：移除 env 重建，sqlite 卷保留。
- 试点=drillcust001（零客户价值租户）：比「新建 cell」更完整地演练
  **存量逐户搬迁+回退保底**全流程（新建 cell 走不到「带着既有数据搬」
  的路径）；drill 卷内只有历轮冒烟产物，风险为零。
- cell 镜像白名单加 `psycopg[binary]>=3.1`。

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| PG 参数类型严格度（TEXT 收非 str 等） | `_coerce` 收口 + 矩阵实跑全暴露 |
| `?`→`%s` 误伤含 `?` 的 SQL 字面量 | 静态扫描断言 + 全矩阵双后端对拍 |
| CTAS/UPDATE FROM 语义差异 | 已核对同形；矩阵含派生/link/增量对拍 |
| 迁移后 drill 冒烟数据不在 | 预期内（新数据根空库起步，实弹重造） |
| psycopg 不在镜像 | Dockerfile 白名单显式加入 |

## 6. 后续（不在本 change）

cell-manager 自动建库发凭据（OpenBAO）、存量逐户搬迁编排、
`pg_database_size` 配额杠杆重估、per-tenant 实例形态决策。
