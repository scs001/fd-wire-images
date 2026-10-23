# wire-dedicated-postgres

## Why

柏讯存储路线已拍板（2026-10-11，docs/ROADMAP.md 方向 A）：柏讯是独立产品条线，
配专用 PostgreSQL。现状痛点：api 元数据分散多个 SQLite 库文件（api/datasets/
demands/ai），生产挂 `/tmp` 共享库是已知脆弱点；SQLite 单写者使同步推送/控制台/
AI 循环并发写互顶；备份恢复停留在"拷文件"。wire-free-tier-data-gate 引入的
api 侧行存储更需要可扩后端。舰队已有完整 PG 配方（fd-postgres 踩坑记录：必须
pg16-trixie 防 collation 降级、shm 512m、SQLAlchemy 2.1 断 psycopg2、布尔聚合
方言、JSON 列形态），本 change 把配方落到柏讯并完成 api 侧双方言保障——
**cell 数据根迁移不在本 change**（独立后续 change），生产滚动是配方就绪后的
运维步骤。

## What Changes

- **wire 专用 PG 实例配方**：`deploy/wire-postgres/` docker compose——pg16-trixie
  钉版、shm 512m、独立卷、健康检查、每日 pg_dump 备份 cron 与恢复演练文档。
  部署目标 cheap-1（与 cells 同机，就近低延迟）；生产部署本身=运维步骤，配方与
  文档先行。
- **api 存储 URL 收敛**：`FD_WIRE_DATABASE_URL`（元数据）与
  `FD_WIRE_DATASETS_DATABASE_URL`（数据集库，含 api 层行存储）双 URL 语义明确，
  均可指 PostgreSQL；dev 默认 SQLite 文件不变（双方言是常态而非迁移特例）。
- **双方言保障**：全部模型与迁移在 SQLite + PG 两方言下可建可查——审计清单
  覆盖 JSON 列（SQLite JSON vs PG JSONB）、布尔聚合（count FILTER 模式，PG 无
  max(boolean)）、时间精度、自增/序列差异；psycopg3 驱钉版进依赖。
- **迁移与回退手册**：生产 SQLite→PG 搬迁步骤（备份→建库→alembic upgrade→
  数据搬运→校验计数→切 env→回退配方），写入 deploy 文档。

## Capabilities

### New Capabilities

- `wire-storage-postgres`: 柏讯专用 PG 存储——实例配方（钉版/卷/备份/恢复）、
  api 双 URL 语义、模型与迁移的双方言保障、生产迁移与回退手册。

## Impact

- **fd-wire/deploy/**：新增 `wire-postgres/`（compose + 备份脚本 + README 运维
  手册）；既有部署文档补 PG 段。
- **fd-wire/api**：config 收敛双 URL、依赖加 psycopg3、双方言审计修复（模型/
  迁移/查询中的 SQLite 特有写法）、测试加 PG 方言冒烟（本地无 PG 时以 SQLAlchemy
  方言编译断言兜底）。
- **不碰**：cell 引擎数据根（DAAS_DATABASE_URL 仍 SQLite，cell 迁 PG 独立 change）、
  生产滚动（运维步骤，需另行确认）、cell-manager。
- **配套**：wire-free-tier-data-gate 的 api 层行存储表纳入本 change 的双方言
  审计范围。
