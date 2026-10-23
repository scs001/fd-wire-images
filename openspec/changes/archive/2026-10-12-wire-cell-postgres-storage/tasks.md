# Tasks: wire-cell-postgres-storage

## 1. 引擎双方言（fd-daas-mcp worktree，internal-main 基线）

- [x] 1.1 新增 `customer-dataset-mcp/db_dialect.py`：`is_postgres()`（URL
  派生+缓存，sqlite 路径零 psycopg import）、`wrap_cursor`（`?`→`%s` 仅
  PG；`PRAGMA table_info` → information_schema 改写并回填 sqlite 形状行）、
  事务头分支（`BEGIN IMMEDIATE`↔`BEGIN`）、WAL DDL 模板
  （AUTOINCREMENT↔GENERATED ALWAYS AS IDENTITY）、异常映射
  （OperationalError↔psycopg 锁类）
- [x] 1.2 `customer_dataset_tools.py` 五个方言点接入适配层：`_tx`、
  `_raw_query_one/all`、`_raw_query_timed`（PG `statement_timeout` 等价+
  同文案超时域错误）、两处 `PRAGMA`、WAL 建表；sqlite 路径行为字节不变
- [x] 1.3 `_coerce` 类型强制收口（PG 严格度；sqlite 路径不感知）

## 2. 测试矩阵（双后端）

- [x] 2.1 `tests/conftest.py` 双后端选择（`FD_ENGINE_TEST_BACKEND` +
  `FD_ENGINE_PG_TEST_URL` 一次性库，未设则 PG 腿跳过）
- [x] 2.2 三个 customer-dataset 测试文件全量双后端通过；新增一致性用例：
  占位符字面量静态扫描、PG 严格类型、超时护栏文案、WAL 自增
- [x] 2.3 sqlite 腿回归零变化（本地引擎 venv 全套 sqlite 跑绿=发布门）

## 3. cell 镜像与配方（fd-wire）

- [x] 3.1 `cell-image/Dockerfile` 依赖白名单 + `psycopg[binary]>=3.1`
- [x] 3.2 `docs/cell-postgres-storage.md`：建库/属主/REVOKE/隔离验证 SQL/
  env 注入/迁移/回退全配方（含红线重述：凭据只归 cell 引擎、平台角色
  无 CONNECT）

## 4. 发布

- [x] 4.1 引擎 commit 推 fd-daas-mcp internal-main（公开仓镜像同步）
- [x] 4.2 fd-wire 快照推 law-ai-official/fd-wire-images；GHA 构 cell 镜像
  （api/web 无改动不滚）；tcr-relay 带 anchor 到 ccr

## 5. 试点（生产）

- [x] 5.1 cheap-3 wire PG：`CREATE ROLE wire_cell_drillcust001` +
  `CREATE DATABASE wire_cell_drillcust001 OWNER …` + `REVOKE CONNECT FROM
  PUBLIC`；隔离验证（api 凭据连 cell 库→拒；属主越库→拒）
- [x] 5.2 cheap-1 drill cell 重建：新 cell 镜像 + env 注入 PG URL（sqlite
  卷不清不删）；启动后 184 工具面/健康检查正常
- [x] 5.3 MCP 实弹（PG 根全行为面）：建集/上传/修正/回滚/派生/link
  （NULL 语义+right_ 前缀）/增量同步/删除清理；行数与口径断言
- [x] 5.4 回退保底验证：sqlite 卷完好性检查 + 回退配方演练（或等价验证）

## 6. 收口

- [x] 6.1 `openspec validate wire-cell-postgres-storage --strict` 过；
  全任务勾选后 `openspec archive wire-cell-postgres-storage`
- [x] 6.2 ROADMAP 方向 A「cell 数据根迁 PG」标注 v1 试点完成；
  记忆更新（含配方坑位）
