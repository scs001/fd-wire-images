# Proposal: wire-cell-postgres-storage — cell 数据根 PostgreSQL 后端（v1：引擎双方言 + drill 试点搬迁）

## Why

存储路线已拍板（2026-10-11，产品负责人）：柏讯单独产品条线 = 专用 PostgreSQL。
api 侧已完成双库 PG 化（wire-storage-postgres：wire_platform / wire_datasets），
**cell 数据根仍是每客户一卷一个 SQLite 文件**——这是中台存储地基的最后一块，
也是 ROADMAP §方向 A「cell 数据根层（真工程）」与 §6 中期第一项
「cell 数据根迁 PG（新 cell 先行、存量逐户）」。

侦察结论（2026-10-12）：迁移距离比预期小——引擎数据访问已走 SQLAlchemy
engine（`DAAS_DATABASE_URL` 任意 URL 入口已在），cell entrypoint 已支持 env
覆盖；真正的 SQLite 专属面收敛为五类：`?` 占位符、`BEGIN IMMEDIATE` 事务头、
`PRAGMA table_info` 两处、WAL 表 `AUTOINCREMENT`、超时护栏
`set_progress_handler`（PG 用 `statement_timeout` 等价）。列类型集
{INTEGER, REAL, TEXT}、ORM 模型、`UPDATE … FROM` / CTAS / `NOT EXISTS`
upsert 全部与 PG 兼容。

## What Changes

1. **引擎可选 PG 数据根（fd-daas-mcp，customer-dataset 组）**：
   `DAAS_DATABASE_URL` 指向 `postgresql+psycopg://…` 即以 PG 为数据根；
   未配置或指向 sqlite 时行为**字节不变**（全部存量 cell 无感）。
2. **双方言行为保障**：customer-dataset 全行为面（建集/上传/修正/派生/
   link/增量同步/删除/事件 WAL）在同一套测试矩阵下双后端执行，差异即缺陷。
3. **cell 镜像与部署配方**：镜像补 `psycopg[binary]` 依赖；
   `docs/cell-postgres-storage.md` 给出「一 cell 一库 + 属主凭据 + 隔离验证 +
   迁移/回退」完整配方。
4. **试点搬迁（drillcust001）**：生产 drill cell 数据根迁 PG
   （库 `wire_cell_drillcust001`，属主凭据只归该 cell 引擎），sqlite 卷原样
   保留为回退保底，MCP 实弹验收。

### 红线落地

- **平台侧永不直读 cell 数据**（红线②）：PG 化后从「约定」升为数据库 ACL
  ——cell 库属主凭据只发该 cell 引擎（env 0600 纪律，与 wgk- 同），平台
  api/管理角色对 cell 库无 CONNECT 权限。
- **存量不动**：不带 PG env 的 cell 升级镜像后数据与行为不变。

### v1 边界（显式不做，留后续 change）

- cell-manager 自动「建库+发凭据」（含凭据入 OpenBAO）——v1 为手工配方+试点；
- 存量客户 cell 逐户搬迁（v1 只做 drill 试点，验证搬迁与回退全流程）；
- 50 万行上限从 SQLite 保护转 `pg_database_size` 商业配额杠杆并重估数字。

## Impact

- **fd-daas-mcp**（internal-main）：customer-dataset-mcp 新增方言适配模块 +
  五个方言点改造 + 测试矩阵；不动謙面本地产品路径（sqlite 默认不变）。
- **fd-wire**：cell-image/Dockerfile（+psycopg）、docs 新配方、本 change 工件；
  api/web 代码**零改动**（本轮无 api/web 镜像发布）。
- **生产**：cheap-3 wire PG 实例新增试点库与属主角色；cheap-1 drill cell
  重建（换 env + 新 cell 镜像）；其余 cell 一概不动。
